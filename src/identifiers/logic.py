__copyright__ = "Copyright 2017 Birkbeck, University of London"
__author__ = "Martin Paul Eve & Andy Byers"
__license__ = "AGPL v3"
__maintainer__ = "Birkbeck Centre for Technology and Publishing"


import datetime
from uuid import uuid4
import requests

from django.urls import reverse
from django.template.loader import render_to_string
from django.utils.http import urlencode
import sys
from utils import models as util_models


def register_crossref_doi(identifier):
    from utils import setting_handler

    domain = identifier.article.journal.domain
    pingback_url = urlencode({'pingback': 'http://{0}{1}'.format(domain, reverse('crossref_pingback'))})

    test_url = 'https://api.crossref.org/deposits?test=true&{0}'.format(pingback_url)
    live_url = 'https://api.crossref.org/deposits?{0}'.format(pingback_url)

    use_crossref = setting_handler.get_setting('Identifiers', 'use_crossref',
                                               identifier.article.journal).processed_value

    if not use_crossref:
        print("[DOI] Not using Crossref DOIs on this journal. Aborting registration.")
        return

    test_mode = setting_handler.get_setting('Identifiers', 'crossref_test', identifier.article.journal).processed_value

    if test_mode:
        util_models.LogEntry.add_entry('Submission', "DOI registration running in test mode", 'Info',
                                       target=identifier.article)
    else:
        util_models.LogEntry.add_entry('Submission', "DOI registration running in live mode", 'Info',
                                       target=identifier.article)

    send_crossref_deposit(test_url if test_mode else live_url, identifier)


def send_crossref_deposit(server, identifier):
    # todo: work out whether this is acceptance or publication
    # if it's acceptance, then we use "0" for volume and issue
    # if publication, then use real values
    # the code here is for acceptance

    from utils import setting_handler
    article = identifier.article

    template_context = {
        'batch_id': uuid4(),
        'timestamp': int(round((datetime.datetime.now() - datetime.datetime(1970, 1, 1)).total_seconds())),
        'depositor_name': setting_handler.get_setting('Identifiers', 'crossref_name',
                                                      identifier.article.journal).processed_value,
        'depositor_email': setting_handler.get_setting('Identifiers', 'crossref_email',
                                                       identifier.article.journal).processed_value,
        'registrant': setting_handler.get_setting('Identifiers', 'crossref_registrant',
                                                  identifier.article.journal).processed_value,
        'journal_title': identifier.article.journal.name,
        'journal_issn': identifier.article.journal.issn,
        'journal_month': identifier.article.date_published.month,
        'journal_day': identifier.article.date_published.day,
        'journal_year': identifier.article.date_published.year,
        'journal_volume': identifier.article.issue.volume,
        'journal_issue': identifier.article.issue.issue,
        'article_title': '{0}{1}{2}'.format(
            identifier.article.title,
            ' ' if identifier.article.subtitle is not None else '',
            identifier.article.subtitle if identifier.article.subtitle is not None else ''),
        'authors': identifier.article.authors.all(),
        'article_month': identifier.article.date_published.month,
        'article_day': identifier.article.date_published.day,
        'article_year': identifier.article.date_published.year,
        'doi': identifier.identifier,
        'article_url': identifier.article.url,
    }

    pdfs = identifier.article.pdfs
    if len(pdfs) > 0:
        template_context['pdf_url'] = article.pdf_url

    template = 'identifiers/crossref.xml'
    crossref_template = render_to_string(template, template_context)

    util_models.LogEntry.add_entry('Submission', "Sending request to {1}: {0}".format(crossref_template, server),
                                   'Info',
                                   target=identifier.article)

    response = requests.post(server, data=crossref_template.encode('utf-8'),
                             auth=(setting_handler.get_setting('Identifiers',
                                                               'crossref_username',
                                                               identifier.article.journal).processed_value,
                                   setting_handler.get_setting('Identifiers', 'crossref_password',
                                                               identifier.article.journal).processed_value),
                             headers={"Content-Type": "application/vnd.crossref.deposit+xml"})

    if response.status_code != 200:
        util_models.LogEntry.add_entry('Error',
                                       "Error depositing: {0}. {1}".format(response.status_code, response.text),
                                       'Debug',
                                       target=identifier.article)
        print("Error depositing: {}".format(response.status_code), file=sys.stderr)
        print(response.text, file=sys.stderr)
    else:
        token = response.json()['message']['batch-id']
        status = response.json()['message']['status']
        util_models.LogEntry.add_entry('Submission', "Deposited {0}. Status: {1}".format(token, status), 'Info',
                                       target=identifier.article)
        print("Status of {} in {}: {}".format(token, identifier.identifier, status))


def create_crossref_doi_identifier(article, doi_suffix=None):
    """ Creates (but does not register remotely) a Crossref DOI

    :param article: the article for which to create the DOI
    :param doi_suffix: an optional DOI suffix
    :return:
    """

    from utils import setting_handler

    if doi_suffix is None:
        doi_suffix = article.id

    doi_prefix = setting_handler.get_setting('Identifiers', 'crossref_prefix', article.journal)

    doi_options = {
        'id_type': 'doi',
        'identifier': '{0}/{1}'.format(doi_prefix, doi_suffix),
        'article': article
    }

    from identifiers import models as identifier_models

    return identifier_models.Identifier.objects.create(doi_options)


def generate_crossref_doi_with_pattern(article):
    """
    Creates a crossref doi utilising a preset pattern.
    :param article: article objects
    :return: returns a DOI
    """

    from utils import setting_handler, render_template

    doi_prefix = setting_handler.get_setting('Identifiers', 'crossref_prefix', article.journal).value
    doi_suffix = render_template.get_requestless_content({'article': article},
                                                         article.journal,
                                                         'doi_pattern',
                                                         group_name='Identifiers')

    doi_options = {
        'id_type': 'doi',
        'identifier': '{0}/{1}'.format(doi_prefix, doi_suffix),
        'article': article
    }

    from identifiers import models as identifier_models

    return identifier_models.Identifier.objects.create(**doi_options)
