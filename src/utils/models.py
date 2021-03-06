__copyright__ = "Copyright 2017 Birkbeck, University of London"
__author__ = "Martin Paul Eve & Andy Byers"
__license__ = "AGPL v3"
__maintainer__ = "Birkbeck Centre for Technology and Publishing"

import os
from uuid import uuid4
import requests

from django.core.serializers import json
from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from hvad.models import TranslatableModel, TranslatedFields
from utils.shared import get_ip_address
from utils import notify

import core.settings as settings
from requests.packages.urllib3.exceptions import InsecureRequestWarning


LOG_TYPES = [
    ('Email', 'Email'),
    ('PageView', 'PageView'),
    ('EditorialAction', 'EditorialAction'),
    ('Error', 'Error'),
    ('Authentication', 'Authentication'),
    ('Submission', 'Submission'),
]

LOG_LEVELS = [
    ('Error', 'Error'),
    ('Debug', 'Debug'),
    ('Info', 'Info'),
]


class LogEntry(models.Model):
    types = models.CharField(max_length=255, null=True, blank=True, choices=LOG_TYPES)
    date = models.DateTimeField(auto_now_add=True)
    description = models.TextField(null=True, blank=True)
    level = models.CharField(max_length=20, null=True, blank=True, choices=LOG_LEVELS)
    actor = models.ForeignKey('core.Account', null=True, blank=True, related_name='actor', on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, related_name='content_type', null=True)
    object_id = models.PositiveIntegerField(blank=True, null=True)
    target = GenericForeignKey('content_type', 'object_id')

    class Meta:
        verbose_name_plural = 'log entries'

    def __str__(self):
        return u'[{0}] {1} - {2}'.format(self.types, self.date, self.description)

    def __repr__(self):
        return u'[{0}] {1} - {2}'.format(self.types, self.date, self.description)

    def add_entry(types, description, level, actor=None, request=None, target=None):

        if actor is not None and callable(getattr(actor, "is_anonymous", None)):
            if actor.is_anonymous():
                actor = None

        kwargs = {
            'types': types,
            'description': description,
            'level': level,
            # if no actor is supplied, assume anonymous
            'actor': actor if actor else None,
            'ip_address': get_ip_address(request),
            'target': target,
        }

        new_entry = LogEntry.objects.create(**kwargs).save()

        if request and request.journal:
            if request.journal.slack_logging_enabled:
                notify.notification(**{'slack_message': '[{0}] {1}'.format(kwargs['ip_address'], description),
                                       'action': ['slack_admins'], 'request': request})

        return new_entry


class Plugin(models.Model):
    name = models.CharField(max_length=200)
    version = models.CharField(max_length=10)
    date_installed = models.DateTimeField(auto_now_add=True)
    enabled = models.BooleanField(default=True)
    display_name = models.CharField(max_length=200, blank=True, null=True)
    press_wide = models.BooleanField(default=False)

    def __str__(self):
        return u'[{0}] {1} - {2}'.format(self.name, self.version, self.enabled)

    def __repr__(self):
        return u'[{0}] {1} - {2}'.format(self.name, self.version, self.enabled)

    def best_name(self):
        if self.display_name:
            return self.display_name

        return self.name


setting_types = (
    ('rich-text', 'Rich Text'),
    ('text', 'Text'),
    ('char', 'Characters'),
    ('number', 'Number'),
    ('boolean', 'Boolean'),
    ('file', 'File'),
    ('select', 'Select'),
    ('json', 'JSON'),
)


class PluginSetting(models.Model):
    name = models.CharField(max_length=100)
    plugin = models.ForeignKey(Plugin)
    types = models.CharField(max_length=20, choices=setting_types, default='Text')
    pretty_name = models.CharField(max_length=100, default='')
    description = models.TextField(null=True, blank=True)
    is_translatable = models.BooleanField(default=False)

    class Meta:
        ordering = ('plugin', 'name')

    def __str__(self):
        return u'%s' % self.name

    def __repr__(self):
        return u'%s' % self.name


class PluginSettingValue(TranslatableModel):
    journal = models.ForeignKey('journal.Journal', blank=True, null=True)
    setting = models.ForeignKey(PluginSetting)

    translations = TranslatedFields(
        value=models.TextField(null=True, blank=True)
    )

    def __repr__(self):
        return "[{0}]: {1}, {2}".format(self.journal.code, self.setting.name, self.value)

    def __str__(self):
        return "[{0}]: {1}".format(self.journal, self.setting.name)

    @property
    def processed_value(self):
        return self.process_value()

    def process_value(self):
        """ Converts string values of settings to proper values

        :return: a value
        """

        if self.setting.types == 'boolean' and self.value == 'on':
            return True
        elif self.setting.types == 'boolean':
            return False
        elif self.setting.types == 'number':
            try:
                return int(self.value)
            except BaseException:
                return 0
        elif self.setting.types == 'json' and self.value:
            return json.loads(self.value)
        else:
            return self.value


class ImportCacheEntry(models.Model):
    url = models.TextField(max_length=800, blank=False, null=False)
    on_disk = models.TextField(max_length=800, blank=False, null=False)
    mime_type = models.CharField(max_length=200, null=True, blank=True)

    @staticmethod
    def nuke():
        for cache in ImportCacheEntry.objects.all():
            os.remove(cache.on_disk)
            cache.delete()

    @staticmethod
    def fetch(url):
        try:
            cached = ImportCacheEntry.objects.get(url=url)

            print("[CACHE] Using cached version of {0}".format(url))

            with open(cached.on_disk, 'rb') as on_disk_file:
                return on_disk_file.read(), cached.mime_type

        except ImportCacheEntry.DoesNotExist:
            print("[CACHE] Fetching remote version of {0}".format(url))

            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/39.0.2171.95 Safari/537.36'}

            # disable SSL checking
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

            fetched = requests.get(url, headers=headers, stream=True, verify=False)

            resp = bytes()

            for chunk in fetched.iter_content(chunk_size=512 * 1024):
                resp += chunk

            # set the filename to a unique UUID4 identifier with the passed file extension
            filename = '{0}'.format(uuid4())

            # set the path to save to be the sub-directory for the article
            path = os.path.join(settings.BASE_DIR, 'files', 'import_cache')

            # create the sub-folders as necessary
            if not os.path.exists(path):
                os.makedirs(path, 0o0775)

            with open(os.path.join(path, filename), 'wb') as f:
                f.write(resp)

            ImportCacheEntry.objects.create(url=url, mime_type=fetched.headers.get('content-type'),
                                            on_disk=os.path.join(path, filename)).save()

            return resp, fetched.headers.get('content-type')

    def __str__(self):
        return self.url
