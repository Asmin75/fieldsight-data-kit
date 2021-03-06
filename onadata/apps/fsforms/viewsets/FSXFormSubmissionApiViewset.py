from __future__ import unicode_literals
import json
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status

from onadata.apps.api.viewsets.xform_submission_api import XFormSubmissionApi
from onadata.apps.eventlog.models import FieldSightLog
from onadata.apps.fieldsight.models import Site
from onadata.apps.fsforms.models import FieldSightXF, Stage, Schedule, SubmissionOfflineSite
from onadata.apps.fsforms.serializers.FieldSightSubmissionSerializer import FieldSightSubmissionSerializer
from ..fieldsight_logger_tools import safe_create_instance
from channels import Group as ChannelGroup
# 10,000,000 bytes
DEFAULT_CONTENT_LENGTH = getattr(settings, 'DEFAULT_CONTENT_LENGTH', 10000000)


def create_instance_from_xml(request, fsid, site, fs_proj_xf, proj_id, xform):
    xml_file_list = request.FILES.pop('xml_submission_file', [])
    xml_file = xml_file_list[0] if len(xml_file_list) else None
    media_files = request.FILES.values()
    return safe_create_instance(fsid, xml_file, media_files, None, request, site, fs_proj_xf, proj_id, xform)


class FSXFormSubmissionApi(XFormSubmissionApi):
    serializer_class = FieldSightSubmissionSerializer
    template_name = 'fsforms/submission.xml'

    def create(self, request, *args, **kwargs):
        if self.request.user.is_anonymous():
            self.permission_denied(self.request)

        fsxfid = kwargs.get('pk', None)
        siteid = kwargs.get('site_id', None)
        offline_submission_site = None
        if fsxfid is None or siteid is None:
            return self.error_response("Site Id Or Form ID Not Given", False, request)
        try:
            fsxfid = int(fsxfid)
            fxf = get_object_or_404(FieldSightXF, pk=kwargs.get('pk'))
            if fxf.project:
                #     project level form hack
                print("redirection project form to project url")
                if siteid == '0':
                    siteid = None
                elif Site.objects.filter(pk=siteid).exists() == False:
                    if len(siteid) > 12:
                        if FieldSightXF.objects.filter(pk=fsxfid).exists():
                            project_form = FieldSightXF.objects.get(pk=fsxfid)
                            project = project_form.project
                            if project:
                                site, created = Site.objects.get_or_create(
                                    identifier="temporary_site",
                                    is_active=True,
                                    name="Temporary  Site",
                                    project_id=project.id,
                                    is_survey=False,
                                )
                                siteid = site.id
                                offline_submission_site, created = SubmissionOfflineSite.objects.get_or_create(
                                    offline_site_id=kwargs.get('site_id', None), temporary_site=site, instance=None,  fieldsight_form=project_form)
                            else:
                                return self.error_response("Invalid Project in Project Form id", False, request)

                        else:
                            return self.error_response("Invalid Form id", False, request)
                    else:
                        return self.error_response("siteid Invalid", False, request)
                if fsxfid is None:
                    return self.error_response("Fieldsight Form ID Not Given", False, request)
                try:
                    fs_proj_xf = get_object_or_404(FieldSightXF, pk=kwargs.get('pk'))
                    xform = fs_proj_xf.xf
                    proj_id = fs_proj_xf.project.id
                    if siteid:
                        site = Site.objects.get(pk=siteid)
                except Exception as e:
                    return self.error_response("Site Id Or Project Form ID Not Vaild", False, request)
                if request.method.upper() == 'HEAD':
                    return Response(status=status.HTTP_204_NO_CONTENT,
                                    headers=self.get_openrosa_headers(request),
                                    template_name=self.template_name)
                error, instance = create_instance_from_xml(request, None, siteid, fs_proj_xf.id, proj_id, xform)

                if error or not instance:
                    return self.error_response(error, False, request)

                if offline_submission_site is not None:
                    try:
                        instance.fieldsight_instance.offline_submission
                    except Exception as e:
                        offline_submission_site.instance = instance.fieldsight_instance
                        offline_submission_site.save()
                        print("new submission")


                if not FieldSightLog.objects.filter(object_id=instance.id, type=16).exists():
                    if fs_proj_xf.is_survey:
                        instance.fieldsight_instance.logs.create(source=self.request.user, type=16,
                                                                 title="new Project level Submission",
                                                                 organization=fs_proj_xf.project.organization,
                                                                 project=fs_proj_xf.project,
                                                                 extra_object=fs_proj_xf.project,
                                                                 extra_message="project",
                                                                 content_object=instance.fieldsight_instance)
                    else:
                        site = Site.objects.get(pk=siteid)
                        instance.fieldsight_instance.logs.create(source=self.request.user, type=16,
                                                                 title="new Site level Submission",
                                                                 organization=fs_proj_xf.project.organization,
                                                                 project=fs_proj_xf.project, site=site,
                                                                 extra_object=site,
                                                                 content_object=instance.fieldsight_instance)

                context = self.get_serializer_context()
                serializer = FieldSightSubmissionSerializer(instance, context=context)
                return Response(serializer.data,
                                headers=self.get_openrosa_headers(request),
                                status=status.HTTP_201_CREATED,
                                template_name=self.template_name)


            # handle of site level form
            fs_proj_xf = fxf.fsform.pk if fxf.fsform else None
            proj_id = fxf.fsform.project.pk if fxf.fsform else fxf.site.project.pk
            xform = fxf.xf
            # site_id = fxf.site.pk if fxf.site else None
        except:
            return self.error_response("Site Id Or Form ID Not Vaild", False, request)



        if request.method.upper() == 'HEAD':
            return Response(status=status.HTTP_204_NO_CONTENT,
                            headers=self.get_openrosa_headers(request),
                            template_name=self.template_name)
        error, instance = create_instance_from_xml(request, fsxfid, siteid, fs_proj_xf, proj_id, xform)
        extra_message = ""

        if error or not instance:
            return self.error_response(error, False, request)

        if fxf.is_staged:
            instance.fieldsight_instance.site.update_current_progress()
        else:
            instance.fieldsight_instance.site.update_status()

        if fxf.is_survey:
            extra_message="project"
        if not FieldSightLog.objects.filter(object_id=instance.id, type=16).exists():
            instance.fieldsight_instance.logs.create(source=self.request.user, type=16, title="new Submission",
                                           organization=instance.fieldsight_instance.site.project.organization,
                                           project=instance.fieldsight_instance.site.project,
                                                            site=instance.fieldsight_instance.site,
                                                            extra_message=extra_message,
                                                            extra_object=instance.fieldsight_instance.site,
                                                            content_object=instance.fieldsight_instance)

        context = self.get_serializer_context()
        serializer = FieldSightSubmissionSerializer(instance, context=context)
        return Response(serializer.data,
                        headers=self.get_openrosa_headers(request),
                        status=status.HTTP_201_CREATED,
                        template_name=self.template_name)


class ProjectFSXFormSubmissionApi(XFormSubmissionApi):
    serializer_class = FieldSightSubmissionSerializer
    template_name = 'fsforms/submission.xml'

    def create(self, request, *args, **kwargs):
        offline_submission_site = None
        if self.request.user.is_anonymous():
            self.permission_denied(self.request)

        fsxfid = kwargs.get('pk', None)
        siteid = kwargs.get('site_id', None)
        if siteid == '0':
            siteid = None
        elif Site.objects.filter(pk=siteid).exists() == False:
            if len(siteid) > 12:
                if FieldSightXF.objects.filter(pk=fsxfid).exists():
                    project_form = FieldSightXF.objects.get(pk=fsxfid)
                    project = project_form.project
                    if project:
                        site, created = Site.objects.get_or_create(
                            identifier="temporary_site",
                            is_active=True,
                            name="Temporary  Site",
                            project_id=project.id,
                            is_survey=False,
                        )
                        siteid = site.id
                        offline_submission_site, created = SubmissionOfflineSite.objects.get_or_create(offline_site_id=kwargs.get('site_id', None), temporary_site=site, instance=None, fieldsight_form=project_form)
                    else:
                        return self.error_response("Invalid Project in Project Form id", False, request)

                else:
                    return self.error_response("Invalid Form id", False, request)
            else:
                return self.error_response("siteid Invalid", False, request)
        if fsxfid is None:
            return self.error_response("Fieldsight Form ID Not Given", False, request)
        try:
            fs_proj_xf = get_object_or_404(FieldSightXF, pk=kwargs.get('pk'))
            xform = fs_proj_xf.xf
            proj_id = fs_proj_xf.project.id
            if siteid:
                site = Site.objects.get(pk=siteid)
        except Exception as e:
            return self.error_response("Site Id Or Project Form ID Not Vaild", False, request)
        if request.method.upper() == 'HEAD':
            return Response(status=status.HTTP_204_NO_CONTENT,
                            headers=self.get_openrosa_headers(request),
                            template_name=self.template_name)
        error, instance = create_instance_from_xml(request, None, siteid, fs_proj_xf.id, proj_id, xform)

        if error or not instance:
            return self.error_response(error, False, request)

        if fs_proj_xf.is_staged and siteid:
            site.update_current_progress()
        elif siteid:
            site.update_status()

        if offline_submission_site is not None:
            try:
                instance.fieldsight_instance.offline_submission
            except Exception as e:
                offline_submission_site.instance = instance.fieldsight_instance
                offline_submission_site.save()
                print("new submission")


        if not FieldSightLog.objects.filter(object_id=instance.id, type=16).exists():
            if fs_proj_xf.is_survey:
                instance.fieldsight_instance.logs.create(source=self.request.user, type=16, title="new Project level Submission",
                                           organization=fs_proj_xf.project.organization,
                                           project=fs_proj_xf.project,
                                                            extra_object=fs_proj_xf.project,
                                                            extra_message="project",
                                                            content_object=instance.fieldsight_instance)
            else:
                site = Site.objects.get(pk=siteid)
                instance.fieldsight_instance.logs.create(source=self.request.user, type=16, title="new Site level Submission",
                                           organization=fs_proj_xf.project.organization,
                                           project=fs_proj_xf.project, site=site,
                                                            extra_object=site,
                                                            content_object=instance.fieldsight_instance)

        context = self.get_serializer_context()
        serializer = FieldSightSubmissionSerializer(instance, context=context)
        return Response(serializer.data,
                        headers=self.get_openrosa_headers(request),
                        status=status.HTTP_201_CREATED,
                        template_name=self.template_name)

