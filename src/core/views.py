__copyright__ = "Copyright 2017 Birkbeck, University of London"
__author__ = "Martin Paul Eve & Andy Byers"
__license__ = "AGPL v3"
__maintainer__ = "Birkbeck Centre for Technology and Publishing"


from importlib import import_module

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, logout, login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.urls import reverse
from django.http import Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.template.defaultfilters import linebreaksbr
from django.utils import timezone
from django.http import HttpResponse
from django.contrib.sessions.models import Session
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.conf import settings as django_settings

from core import models, forms, files, logic
from security.decorators import editor_user_required, article_author_required
from submission import models as submission_models
from review import models as review_models
from copyediting import models as copyedit_models
from production import models as production_models
from journal import models as journal_models
from proofing import logic as proofing_logic
from proofing import models as proofing_models
from utils import models as util_models, setting_handler, orcid

from django.db.models import Q


def user_login(request):
    if request.user.is_authenticated():
        messages.info(request, 'You are already logged in.')
        if request.GET.get('next'):
            return redirect(request.GET.get('next'))
        else:
            return redirect(reverse('website_index'))
    else:
        bad_logins = logic.check_for_bad_login_attempts(request)

    if bad_logins >= 5:
        messages.add_message(request, messages.ERROR, 'You have been banned from logging in due to failed attempts.')
        return redirect(reverse('website_index'))

    form = forms.LoginForm(bad_logins=bad_logins)

    if request.POST:
        form = forms.LoginForm(request.POST, bad_logins=bad_logins)

        if form.is_valid():
            user = request.POST.get('user_name').lower()
            pawd = request.POST.get('user_pass')

            user = authenticate(username=user, password=pawd)

            if user is not None:
                if user.is_active:
                    login(request, user)
                    messages.info(request, 'Login successful.')
                    logic.clear_bad_login_attempts(request)

                    orcid_token = request.POST.get('orcid_token', None)
                    if orcid_token:
                        try:
                            token_obj = models.OrcidToken.objects.get(token=orcid_token, expiry__gt=timezone.now())
                            user.orcid = token_obj.orcid
                            user.save()
                            token_obj.delete()
                        except models.OrcidToken.DoesNotExist:
                            pass

                    if request.GET.get('next'):
                        return redirect(request.GET.get('next'))
                    else:
                        return redirect(reverse('website_index'))
                else:
                    messages.add_message(request, messages.ERROR, 'User account is not active.')
            else:
                messages.add_message(request, messages.ERROR, 'Account not found with those details.')
                util_models.LogEntry.add_entry(types='Authentication',
                                               description='Failed login attempt for user {0}'.format(
                                                   request.POST.get('user_name')),
                                               level='Info', actor=None, request=request)
                logic.add_failed_login_attempt(request)

    context = {
        'form': form,
    }
    template = 'core/login.html'

    return render(request, template, context)


def user_login_orcid(request):
    orcid_code = request.GET.get('code', None)

    if orcid_code:
        auth = orcid.retrieve_tokens(orcid_code, domain=request.journal_base_url)
        orcid_id = auth.get('orcid', None)

        if orcid_id:
            try:
                user = models.Account.objects.get(orcid=orcid_id)
                user.backend = 'django.contrib.auth.backends.ModelBackend'
                login(request, user)

                if request.GET.get('next'):
                    return redirect(request.GET.get('next'))
                else:
                    return redirect(reverse('core_dashboard'))
            except models.Account.DoesNotExist:
                # Set Token and Redirect
                models.OrcidToken.objects.filter(orcid=orcid_id).delete()
                new_token = models.OrcidToken.objects.create(orcid=orcid_id)
                return redirect(reverse('core_orcid_registration', kwargs={'token': new_token.token}))
        else:
            messages.add_message(
                request,
                messages.WARNING,
                'Valid ORCiD not returned, please try again, or login with your username and password.'
            )
            return redirect(reverse('core_login'))
    else:
        messages.add_message(
            request,
            messages.WARNING,
            'No authorisation code provided, please try again or login with your username and password.')
        return redirect(reverse('core_login'))


@login_required
def user_logout(request):
    messages.info(request, 'You have been logged out.')
    logout(request)
    return redirect(reverse('website_index'))


def get_reset_token(request):
    new_reset_token = None

    if request.POST:
        email_address = request.POST.get('email_address')

        try:
            account = models.Account.objects.get(email__iexact=email_address)
            # Expire any existing tokens for this user
            models.PasswordResetToken.objects.filter(account=account).update(expired=True)

            # Create a new token
            new_reset_token = models.PasswordResetToken.objects.create(account=account)
            logic.send_reset_token(request, new_reset_token)
            return redirect(reverse('core_login'))
        except models.Account.DoesNotExist:
            return redirect(reverse('core_login'))

    template = 'core/accounts/get_reset_token.html'
    context = {
        'new_reset_token': new_reset_token,
    }

    return render(request, template, context)


def reset_password(request, token):
    reset_token = get_object_or_404(models.PasswordResetToken, token=token, expired=False)
    form = forms.PasswordResetForm()

    if reset_token.has_expired():
        raise Http404

    if request.POST:
        form = forms.PasswordResetForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data['password_2']
            reset_token.account.set_password(password)
            reset_token.account.save()
            reset_token.expired = True
            reset_token.save()
            messages.add_message(request, messages.SUCCESS, 'Your password has been reset.')
            return redirect(reverse('core_login'))

    template = 'core/accounts/reset_password.html'
    context = {
        'reset_token': reset_token,
        'form': form,
    }

    return render(request, template, context)


def register(request):
    token, token_obj = request.GET.get('token', None), None
    if token:
        token_obj = get_object_or_404(models.OrcidToken, token=token)

    form = forms.RegistrationForm()

    if request.POST:
        form = forms.RegistrationForm(request.POST)

        if form.is_valid():
            if token_obj:
                new_user = form.save(commit=False)
                new_user.orcid = token_obj.orcid
                new_user.save()
                token_obj.delete()
            else:
                new_user = form.save()

            if request.journal:
                new_user.add_account_role('author', request.journal)
            logic.send_confirmation_link(request, new_user)

            messages.add_message(request, messages.SUCCESS, 'Your account has been created, please follow the'
                                                            'instructions in the email that has been sent to you.')
            return redirect(reverse('core_login'))

    template = 'core/accounts/register.html'
    context = {
        'form': form,
    }

    return render(request, template, context)


def orcid_registration(request, token):
    token = get_object_or_404(models.OrcidToken, token=token, expiry__gt=timezone.now())

    template = 'core/accounts/orcid_registration.html'
    context = {
        'token': token,
    }

    return render(request, template, context)


def activate_account(request, token):
    try:
        account = models.Account.objects.get(confirmation_code=token, is_active=False)
        account.is_active = True
        account.confirmation_code = None
        account.save()
    except models.Account.DoesNotExist:
        account = None

    template = 'core/accounts/activate_account.html'
    context = {
        'account': account,
    }

    return render(request, template, context)


@login_required
def edit_profile(request):
    user = request.user

    form = forms.EditAccountForm(instance=user)

    if request.POST:
        if 'email' in request.POST:
            email_address = request.POST.get('email_address')
            try:
                validate_email(email_address)
                logic.handle_email_change(request, email_address)
                return redirect(reverse('website_index'))
            except ValidationError:
                messages.add_message(request, messages.WARNING, 'Email address is not valid.')

        elif 'edit_profile' in request.POST:
            form = forms.EditAccountForm(request.POST, request.FILES, instance=user)

            if form.is_valid():
                form.save()
                messages.add_message(request, messages.SUCCESS, 'Profile updated.')
                return redirect(reverse('core_edit_profile'))

    template = 'core/accounts/edit_profile.html'
    context = {
        'form': form,
        'user_to_edit': user,
    }

    return render(request, template, context)


@login_required
def dashboard(request):
    template = 'core/dashboard.html'
    new_proofing, active_proofing, completed_proofing, new_proofing_typesetting, active_proofing_typesetting, \
        completed_proofing_typesetting = proofing_logic.get_tasks(request)
    section_editor_articles = review_models.EditorAssignment.objects.filter(editor=request.user,
                                                                            editor_type='section-editor',
                                                                            article__journal=request.journal)

    # TODO: Move most of this to model logic.
    context = {
        'new_proofing': new_proofing.count(),
        'active_proofing': active_proofing.count(),
        'completed_proofing': completed_proofing.count(),
        'new_proofing_typesetting': new_proofing_typesetting.count(),
        'completed_proofing_typesetting': completed_proofing_typesetting.count(),
        'active_proofing_typesetting': active_proofing_typesetting.count(),
        'unassigned_articles_count': submission_models.Article.objects.filter(
            stage=submission_models.STAGE_UNASSIGNED, journal=request.journal).count(),
        'assigned_articles_count': submission_models.Article.objects.filter(
            Q(stage=submission_models.STAGE_ASSIGNED) | Q(stage=submission_models.STAGE_UNDER_REVIEW) | Q(
                stage=submission_models.STAGE_UNDER_REVISION), journal=request.journal).count(),
        'editing_articles_count': submission_models.Article.objects.filter(
            Q(stage=submission_models.STAGE_EDITOR_COPYEDITING) | Q(
                stage=submission_models.STAGE_AUTHOR_COPYEDITING) | Q(
                stage=submission_models.STAGE_FINAL_COPYEDITING), journal=request.journal).count(),
        'production_articles_count': submission_models.Article.objects.filter(
            Q(stage=submission_models.STAGE_TYPESETTING), journal=request.journal).count(),
        'proofing_articles_count': submission_models.Article.objects.filter(
            Q(stage=submission_models.STAGE_PROOFING), journal=request.journal).count(),
        'prepub_articles_count': submission_models.Article.objects.filter(
            Q(stage=submission_models.STAGE_READY_FOR_PUBLICATION), journal=request.journal).count(),
        'is_editor': request.user.is_editor(request),
        'is_author': request.user.is_author(request),
        'is_reviewer': request.user.is_reviewer(request),
        'section_editor_articles': section_editor_articles,
        'active_submission_count': submission_models.Article.objects.filter(
            owner=request.user,
            journal=request.journal).exclude(
            stage=submission_models.STAGE_UNSUBMITTED).count(),
        'in_progress_submission_count': submission_models.Article.objects.filter(owner=request.user,
                                                                                 journal=request.journal,
                                                                                 stage=submission_models.
                                                                                 STAGE_UNSUBMITTED).count(),
        'assigned_articles_for_user_review_count': review_models.ReviewAssignment.objects.filter(
            Q(is_complete=False) &
            Q(reviewer=request.user) &
            Q(article__stage=submission_models.STAGE_UNDER_REVIEW) &
            Q(date_accepted__isnull=True), article__journal=request.journal).count(),
        'assigned_articles_for_user_review_accepted_count': review_models.ReviewAssignment.objects.filter(
            Q(is_complete=False) &
            Q(reviewer=request.user) &
            Q(article__stage=submission_models.STAGE_UNDER_REVIEW) &
            Q(date_accepted__isnull=False), article__journal=request.journal).count(),
        'assigned_articles_for_user_review_completed_count': review_models.ReviewAssignment.objects.filter(
            Q(is_complete=True) &
            Q(reviewer=request.user) &
            Q(date_declined__isnull=False), article__journal=request.journal).count(),

        'copyeditor_requests': copyedit_models.CopyeditAssignment.objects.filter(
            Q(copyeditor=request.user) &
            Q(decision__isnull=True) &
            Q(copyedit_reopened__isnull=True), article__journal=request.journal).count(),
        'copyeditor_accepted_requests': copyedit_models.CopyeditAssignment.objects.filter(
            Q(copyeditor=request.user, decision='accept', copyeditor_completed__isnull=True,
              article__journal=request.journal) |
            Q(copyeditor=request.user, decision='accept', copyeditor_completed__isnull=False,
              article__journal=request.journal, copyedit_reopened__isnull=False,
              copyedit_reopened_complete__isnull=True)
        ).count(),
        'copyeditor_completed_requests': copyedit_models.CopyeditAssignment.objects.filter(
            (Q(copyeditor=request.user) & Q(copyeditor_completed__isnull=False)) |
            (Q(copyeditor=request.user) & Q(copyeditor_completed__isnull=False) &
             Q(copyedit_reopened_complete__isnull=False)), article__journal=request.journal).count(),

        'typeset_tasks': production_models.TypesetTask.objects.filter(
            assignment__article__journal=request.journal,
            accepted__isnull=True,
            completed__isnull=True).count(),
        'typeset_in_progress_tasks': production_models.TypesetTask.objects.filter(
            assignment__article__journal=request.journal,
            accepted__isnull=False,
            completed__isnull=True).count(),
        'typeset_completed_tasks': production_models.TypesetTask.objects.filter(
            assignment__article__journal=request.journal,
            accepted__isnull=False,
            completed__isnull=False).count(),
        'active_submissions': submission_models.Article.objects.filter(owner=request.user,
                                                                       journal=request.journal).exclude(
            stage=submission_models.STAGE_UNSUBMITTED).order_by('-date_submitted'),
        'progress_submissions': submission_models.Article.objects.filter(
            journal=request.journal,
            owner=request.user,
            stage=submission_models.STAGE_UNSUBMITTED).order_by('-date_started')
    }

    return render(request, template, context)


@article_author_required
def dashboard_article(request, article_id):
    article = get_object_or_404(submission_models.Article, pk=article_id)

    template = 'core/article.html'
    context = {
        'article': article,
    }

    return render(request, template, context)


@editor_user_required
def manager_index(request):
    if not request.journal:
        from press import views as press_views
        return press_views.manager_index(request)

    template = 'core/manager/index.html'
    context = {}

    return render(request, template, context)


@staff_member_required
def flush_cache(request):
    cache.clear()
    messages.add_message(request, messages.SUCCESS, 'Memcached has been flushed.')

    return redirect(reverse('core_manager_index'))


@editor_user_required
def settings_index(request):
    template = 'core/manager/settings/index.html'
    context = {
        'settings': [{group.name: models.Setting.objects.filter(group=group).order_by('name')} for group in
                     models.SettingGroup.objects.all().order_by('name')],
    }

    return render(request, template, context)


@editor_user_required
def edit_setting(request, setting_group, setting_name):
    setting_value = setting_handler.get_setting(setting_group, setting_name, request.journal, create=True)

    if setting_value.setting.types == 'rich-text':
        setting_value.value = linebreaksbr(setting_value.value)

    edit_form = forms.EditKey(key_type=setting_value.setting.types, value=setting_value.value)

    if request.POST and 'delete' in request.POST:
        setting_value.value = ''
        setting_value.save()

        return redirect(reverse('core_settings_index'))

    if request.POST:
        value = request.POST.get('value')
        if request.FILES:
            value = handle_file(request, request.FILES['value'])

        setting_value.value = value
        setting_value.save()

        cache.clear()

        return redirect(reverse('core_settings_index'))

    template = 'core/manager/settings/edit_setting.html'
    context = {
        'setting': setting_value.setting,
        'group': setting_value.setting.group,
        'edit_form': edit_form,
    }

    return render(request, template, context)


@editor_user_required
def edit_settings_group(request, group):
    if request.journal:
        journal_id = None
        journal = request.journal
    else:
        journal_id = request.GET.get('journal')
        journal = get_object_or_404(journal_models.Journal, pk=journal_id)
        # Set request.journal
        request.journal = journal

    settings, setting_group = logic.get_settings_to_edit(group, journal)

    if not settings:
        raise Http404

    edit_form = forms.GeneratedSettingForm(settings=settings)
    attr_form = forms.JournalAttributeForm(instance=journal)

    if request.POST:
        edit_form = forms.GeneratedSettingForm(request.POST, settings=settings)
        attr_form = forms.JournalAttributeForm(request.POST, request.FILES, instance=journal)

        if edit_form.is_valid() and attr_form.is_valid():
            edit_form.save(group=setting_group, journal=journal)
            attr_form.save()
            logic.handle_default_thumbnail(request, journal, attr_form)
            logic.handle_press_override_image(request, journal, attr_form)

            if group == 'journal' and journal.default_large_image:
                path = django_settings.BASE_DIR + journal.default_large_image.url
                logic.resize_and_crop(path, [750, 324], 'middle')

            cache.clear()

            # Unset request.journal
            if journal_id:
                request.journal = None

            if request.journal:
                return redirect(reverse('core_edit_settings_group', kwargs={'group': group}))
            else:
                return redirect("{0}?journal={1}".format(reverse('core_edit_settings_group', kwargs={'group': group}),
                                                         journal_id))

    template = 'core/manager/settings/group.html'
    context = {
        'group': group,
        'settings': settings,
        'edit_form': edit_form,
        'attr_form': attr_form,
    }

    return render(request, template, context)


@editor_user_required
def edit_plugin_settings_groups(request, plugin, setting_group_name, journal=None, title=None):
    if journal != '0':
        journal = journal_models.Journal.objects.get(fk=int(journal))
    else:
        journal = None

    from utils import models as utils_models
    plugin = utils_models.Plugin.objects.get(name=plugin)

    module_name = "{0}.{1}.plugin_settings".format("plugins", plugin.name)
    plugin_settings = import_module(module_name)
    manager_url = getattr(plugin_settings, 'MANAGER_URL', '')
    settings = getattr(plugin_settings, setting_group_name, '')()

    if not settings:
        raise Http404

    edit_form = forms.GeneratedPluginSettingForm(settings=settings)

    if request.POST:
        edit_form = forms.GeneratedPluginSettingForm(request.POST, settings=settings)

        if edit_form.is_valid():
            edit_form.save(plugin=plugin, journal=journal)
            cache.clear()

            return redirect(reverse(request.GET['return']))

    if not title:
        title = plugin.best_name()

    template = 'core/manager/settings/plugin.html'
    context = {
        'plugin': plugin,
        'settings': settings,
        'edit_form': edit_form,
        'title': title,
        'manager_url': manager_url,
    }

    return render(request, template, context)


@editor_user_required
def roles(request):
    template = 'core/manager/roles/roles.html'
    context = {
        'roles': models.Role.objects.all(),
    }

    return render(request, template, context)


@editor_user_required
def role(request, slug):
    role_obj = get_object_or_404(models.Role, slug=slug)

    account_roles = models.AccountRole.objects.filter(journal=request.journal, role=role_obj)
    users_with_role = [assignment.user.pk for assignment in account_roles]
    user_list = models.Account.objects.all().order_by('last_name').exclude(pk__in=users_with_role)

    template = 'core/manager/roles/role.html'
    context = {
        'role': role_obj,
        'users': user_list,
        'account_roles': account_roles
    }

    return render(request, template, context)


@editor_user_required
def role_action(request, slug, user_id, action):
    user = get_object_or_404(models.Account, pk=user_id)
    role_obj = get_object_or_404(models.Role, slug=slug)

    if action == 'add':
        user.add_account_role(role_slug=slug, journal=request.journal)
    elif action == 'remove':
        user.remove_account_role(role_slug=slug, journal=request.journal)

    user.save()

    return redirect(reverse('core_manager_role', kwargs={'slug': role_obj.slug}))


@editor_user_required
def users(request):
    if request.POST:
        users = request.POST.getlist('users')
        role = request.POST.get('role')
        logic.handle_add_users_to_role(users, role, request)
        return redirect(reverse('core_manager_users'))

    template = 'core/manager/users/index.html'
    context = {
        'users': models.Account.objects.filter(is_active=True),
        'roles': models.Role.objects.all().order_by(('name')),
    }
    return render(request, template, context)


@editor_user_required
def add_user(request):
    form = forms.EditAccountForm()
    registration_form = forms.AdminUserForm(active='add')
    return_url = request.GET.get('return', None)
    role = request.GET.get('role', None)

    if request.POST:
        registration_form = forms.AdminUserForm(request.POST, active='add')

        if registration_form.is_valid():
            new_user = registration_form.save()
            if role:
                new_user.add_account_role(role, request.journal)

            form = forms.EditAccountForm(request.POST, request.FILES, instance=new_user)
            if form.is_valid():
                form.save()
                messages.add_message(request, messages.SUCCESS, 'User created.')

                if return_url:
                    return redirect(return_url)

                return redirect(reverse('core_manager_users'))

    template = 'core/manager/users/edit.html'
    context = {
        'form': form,
        'registration_form': registration_form,
        'active': 'add',
    }
    return render(request, template, context)


@editor_user_required
def user_edit(request, user_id):
    user = models.Account.objects.get(pk=user_id)
    form = forms.EditAccountForm(instance=user)
    registration_form = forms.AdminUserForm(instance=user)

    if request.POST:
        form = forms.EditAccountForm(request.POST, request.FILES, instance=user)
        registration_form = forms.AdminUserForm(request.POST, instance=user)

        if form.is_valid() and registration_form.is_valid():
            registration_form.save()
            form.save()
            messages.add_message(request, messages.SUCCESS, 'Profile updated.')

            return redirect(reverse('core_manager_users'))

    template = 'core/manager/users/edit.html'
    context = {
        'user_to_edit': user,
        'form': form,
        'registration_form': registration_form,
        'active': 'update',
    }
    return render(request, template, context)


@editor_user_required
def inactive_users(request):
    user_list = models.Account.objects.filter(is_active=False)

    template = 'core/manager/users/inactive.html'
    context = {
        'users': user_list,
    }

    return render(request, template, context)


@staff_member_required
def logged_in_users(request):
    """
    Gets a list of authenticated users whose sessions have not expired yet.
    :param request: django request object
    :return: contextualised django template
    """
    sessions = Session.objects.filter(expire_date__gte=timezone.now())
    user_id_list = list()

    for session in sessions:
        data = session.get_decoded()
        user_id_list.append(data.get('_auth_user_id', None))

    users = models.Account.objects.filter(id__in=user_id_list)

    template = 'core/manager/users/logged_in_users.html'
    context = {
        'users': users,
    }

    return render(request, template, context)


@editor_user_required
def settings_home(request):
    # this should return a context containing:
    # 1. An excluded list of homepage items
    # 2. A list of active homepage items

    active_elements = models.HomepageElement.objects.filter(content_type=request.model_content_type,
                                                            object_id=request.site_type.pk, active=True)
    active_pks = [f.pk for f in active_elements.all()]

    if request.press and not request.journal:
        elements = models.HomepageElement.objects.filter(content_type=request.model_content_type,
                                                         object_id=request.site_type.pk,
                                                         available_to_press=True).exclude(pk__in=active_pks)
    else:
        elements = models.HomepageElement.objects.filter(content_type=request.model_content_type,
                                                         object_id=request.site_type.pk).exclude(pk__in=active_pks)

    if 'add' in request.POST:
        element_id = request.POST.get('add')
        homepage_element = get_object_or_404(models.HomepageElement, pk=element_id,
                                             content_type=request.model_content_type, object_id=request.site_type.pk)

        homepage_element.active = True
        homepage_element.save()

        return redirect(reverse('home_settings_index'))

    if 'delete' in request.POST:
        element_id = request.POST.get('delete')
        homepage_element = get_object_or_404(models.HomepageElement, pk=element_id,
                                             content_type=request.model_content_type, object_id=request.site_type.pk)

        homepage_element.active = False
        homepage_element.save()

        return redirect(reverse('home_settings_index'))

    template = 'core/manager/settings/index_home.html'
    context = {
        'active_elements': active_elements,
        'elements': elements,
    }

    return render(request, template, context)


@editor_user_required
def journal_home_order(request):
    if request.POST:
        ids = request.POST.getlist('element[]')
        ids = [int(_id) for _id in ids]

        for he in models.HomepageElement.objects.filter(content_type=request.model_content_type,
                                                        object_id=request.site_type.pk, active=True):
            he.sequence = ids.index(he.pk)
            he.save()

    return HttpResponse('Thanks')


@editor_user_required
def article_images(request):
    articles = submission_models.Article.objects.filter(journal=request.journal)

    template = 'core/manager/images/articles.html'
    context = {
        'articles': articles,
    }

    return render(request, template, context)


@editor_user_required
def article_image_edit(request, article_pk):
    article = get_object_or_404(submission_models.Article, pk=article_pk, journal=request.journal)
    article_meta_image_form = forms.ArticleMetaImageForm(instance=article)

    if 'delete' in request.POST:
        delete_id = request.POST.get('delete')
        file_to_delete = get_object_or_404(models.File, pk=delete_id, article_id=article_pk)
        article_files = [article.thumbnail_image_file, article.large_image_file]

        if file_to_delete in article_files and request.user.is_staff or request.user == file_to_delete.owner:
            file_to_delete.delete()

        return redirect(reverse('core_article_image_edit', kwargs={'article_pk': article.pk}))

    if request.POST and request.FILES and 'large' in request.POST:
        uploaded_file = request.FILES.get('image_file')
        logic.handle_article_large_image_file(uploaded_file, article, request)

    elif request.POST and request.FILES and 'thumb' in request.POST:
        uploaded_file = request.FILES.get('image_file')
        logic.handle_article_thumb_image_file(uploaded_file, article, request)

    elif request.POST and request.FILES and 'meta' in request.POST:
        article.unlink_meta_file()
        article_meta_image_form = forms.ArticleMetaImageForm(request.POST, request.FILES, instance=article)
        if article_meta_image_form.is_valid():
            article_meta_image_form.save()

    if request.POST:
        return redirect(reverse('core_article_image_edit', kwargs={'article_pk': article.pk}))

    template = 'core/manager/images/article_image.html'
    context = {
        'article': article,
        'article_meta_image_form': article_meta_image_form,
    }

    return render(request, template, context)


@editor_user_required
def contacts(request):
    form = forms.JournalContactForm()
    contacts = models.Contacts.objects.filter(content_type=request.model_content_type, object_id=request.site_type.pk)

    if 'delete' in request.POST:
        contact_id = request.POST.get('delete')
        contact = get_object_or_404(models.Contacts,
                                    pk=contact_id,
                                    content_type=request.model_content_type,
                                    object_id=request.site_type.pk)
        contact.sequence = request.journal.next_contact_order()
        contact.delete()
        return redirect(reverse('core_journal_contacts'))

    if request.POST:
        form = forms.JournalContactForm(request.POST)

        if form.is_valid():
            contact = form.save(commit=False)
            contact.content_type = request.model_content_type
            contact.object_id = request.site_type.pk
            contact.save()
            return redirect(reverse('core_journal_contacts'))

    template = 'core/manager/contacts/index.html'
    context = {
        'form': form,
        'contacts': contacts,
        'action': 'new',
    }

    return render(request, template, context)


@editor_user_required
def edit_contacts(request, contact_id):
    contact = get_object_or_404(models.Contacts,
                                pk=contact_id,
                                content_type=request.model_content_type,
                                object_id=request.site_type.pk)
    form = forms.JournalContactForm(instance=contact)
    contacts = models.Contacts.objects.filter(content_type=request.model_content_type, object_id=request.site_type.pk)

    if request.POST:
        form = forms.JournalContactForm(request.POST, instance=contact)

        if form.is_valid():
            form.save()
            return redirect(reverse('core_journal_contacts'))

    template = 'core/manager/contacts/index.html'
    context = {
        'form': form,
        'contacts': contacts
    }

    return render(request, template, context)


@editor_user_required
def contacts_order(request):
    if request.POST:
        ids = request.POST.getlist('contact[]')
        ids = [int(_id) for _id in ids]

        for jc in models.Contacts.objects.filter(content_type=request.model_content_type, object_id=request.site_type.pk):
            jc.sequence = ids.index(jc.pk)
            jc.save()

    return HttpResponse('Thanks')


@editor_user_required
def editorial_team(request):
    editorial_groups = models.EditorialGroup.objects.filter(journal=request.journal)
    form = forms.EditorialGroupForm(next_sequence=request.journal.next_group_order())

    if 'delete' in request.POST:
        delete_id = request.POST.get('delete')
        group = get_object_or_404(models.EditorialGroup, pk=delete_id, journal=request.journal)
        group.delete()
        return redirect(reverse('core_editorial_team'))

    if request.POST:
        form = forms.EditorialGroupForm(request.POST)

        if form.is_valid():
            group = form.save(commit=False)
            group.journal = request.journal
            group.save()

            return redirect(reverse('core_editorial_team'))

    template = 'core/manager/editorial/index.html'
    context = {
        'editorial_groups': editorial_groups,
        'form': form,
    }

    return render(request, template, context)


@editor_user_required
def edit_editorial_group(request, group_id):
    editorial_groups = models.EditorialGroup.objects.filter(journal=request.journal)
    group = get_object_or_404(models.EditorialGroup, pk=group_id, journal=request.journal)
    form = forms.EditorialGroupForm(instance=group)

    if request.POST:
        form = forms.EditorialGroupForm(request.POST, instance=group)
        if form.is_valid():
            form.save()
            return redirect(reverse('core_editorial_team'))

    template = 'core/manager/editorial/index.html'
    context = {
        'group': group,
        'editorial_groups': editorial_groups,
        'form': form,
    }

    return render(request, template, context)


@editor_user_required
def add_member_to_group(request, group_id, user_id=None):
    group = get_object_or_404(models.EditorialGroup, pk=group_id, journal=request.journal)
    member_pks = [member.user.pk for member in group.editorialgroupmember_set.all()]
    user_list = models.Account.objects.exclude(pk__in=member_pks)

    if 'delete' in request.POST:
        delete_id = request.POST.get('delete')
        membership = get_object_or_404(models.EditorialGroupMember, pk=delete_id)
        membership.delete()
        return redirect(reverse('core_editorial_member_to_group', kwargs={'group_id': group.pk}))

    if user_id:
        user_to_add = get_object_or_404(models.Account, pk=user_id)
        if user_id not in member_pks:
            models.EditorialGroupMember.objects.create(group=group,
                                                       user=user_to_add,
                                                       sequence=group.next_member_sequence())
        return redirect(reverse('core_editorial_member_to_group', kwargs={'group_id': group.pk}))

    template = 'core/manager/editorial/add_member.html'
    context = {
        'group': group,
        'users': user_list,
    }

    return render(request, template, context)


@staff_member_required
def plugin_list(request):

    plugin_list = list()

    if request.journal:
        plugins = util_models.Plugin.objects.filter(enabled=True)
    else:
        plugins = util_models.Plugin.objects.filter(enabled=True, press_wide=True)

    for plugin in plugins:
        try:
            module_name = "{0}.{1}.plugin_settings".format("plugins", plugin.name)
            plugin_settings = import_module(module_name)
            plugin_list.append({'model': plugin,
                                'manager_url': getattr(plugin_settings, 'MANAGER_URL', ''),
                                'name': getattr(plugin_settings, 'PLUGIN_NAME')
                                })
        except ImportError:
            pass

    template = 'core/manager/plugins.html'
    context = {
        'plugins': plugin_list,
    }

    return render(request, template, context)


@editor_user_required
@editor_user_required
def editorial_ordering(request, type_to_order, group_id=None):
    if type_to_order == 'group':
        ids = request.POST.getlist('group[]')
        objects = models.EditorialGroup.objects.filter(journal=request.journal)
    elif type_to_order == 'sections':
        ids = request.POST.getlist('section[]')
        objects = submission_models.Section.objects.filter(journal=request.journal)
    else:
        group = get_object_or_404(models.EditorialGroup, pk=group_id, journal=request.journal)
        ids = request.POST.getlist('member[]')
        objects = models.EditorialGroupMember.objects.filter(group=group)

    ids = [int(_id) for _id in ids]

    for _object in objects:
        _object.sequence = ids.index(_object.pk)
        _object.save()

    return HttpResponse('Thanks')


@editor_user_required
def kanban(request):
    unassigned_articles = submission_models.Article.objects.filter(Q(stage=submission_models.STAGE_UNASSIGNED),
                                                                   journal=request.journal) \
        .order_by('-date_submitted')

    in_review = submission_models.Article.objects.filter(Q(stage=submission_models.STAGE_ASSIGNED) |
                                                         Q(stage=submission_models.STAGE_UNDER_REVIEW) |
                                                         Q(stage=submission_models.STAGE_UNDER_REVISION),
                                                         journal=request.journal) \
        .order_by('-date_submitted')

    copyediting = submission_models.Article.objects.filter(Q(stage=submission_models.STAGE_ACCEPTED) |
                                                           Q(stage__in=submission_models.COPYEDITING_STAGES),
                                                           journal=request.journal) \
        .order_by('-date_submitted')

    assigned_table = production_models.ProductionAssignment.objects.filter(article__journal=request.journal)
    assigned = [assignment.article.pk for assignment in assigned_table]

    prod_unassigned_articles = submission_models.Article.objects.filter(
        stage=submission_models.STAGE_TYPESETTING, journal=request.journal).exclude(
        id__in=assigned)
    assigned_articles = submission_models.Article.objects.filter(stage=submission_models.STAGE_TYPESETTING,
                                                                 journal=request.journal).exclude(
        id__in=unassigned_articles)

    proofing_assigned_table = proofing_models.ProofingAssignment.objects.filter(article__journal=request.journal)
    proofing_assigned = [assignment.article.pk for assignment in proofing_assigned_table]

    proof_unassigned_articles = submission_models.Article.objects.filter(
        stage=submission_models.STAGE_PROOFING, journal=request.journal).exclude(
        id__in=proofing_assigned)
    proof_assigned_articles = submission_models.Article.objects.filter(stage=submission_models.STAGE_PROOFING,
                                                                       journal=request.journal).exclude(
        id__in=proof_unassigned_articles)

    prepub = submission_models.Article.objects.filter(Q(stage=submission_models.STAGE_READY_FOR_PUBLICATION),
                                                      journal=request.journal) \
        .order_by('-date_submitted')

    context = {
        'unassigned_articles': unassigned_articles,
        'in_review': in_review,
        'copyediting': copyediting,
        'production': prod_unassigned_articles,
        'production_assigned': assigned_articles,
        'proofing': proof_unassigned_articles,
        'proofing_assigned': proof_assigned_articles,
        'prepubs': prepub,
    }

    template = 'core/kanban.html'

    return render(request, template, context)


@editor_user_required
def delete_note(request, article_id, note_id):
    note = get_object_or_404(submission_models.Note, pk=note_id)
    note.delete()

    url = reverse('kanban_home')

    return redirect("{0}?article_id={1}".format(url, article_id))


@staff_member_required
def manage_notifications(request, notification_id=None):
    notifications = journal_models.Notifications.objects.filter(journal=request.journal)
    notification = None
    form = forms.NotificationForm()

    if notification_id:
        notification = get_object_or_404(journal_models.Notifications, pk=notification_id)
        form = forms.NotificationForm(instance=notification)

    if request.POST:
        if 'delete' in request.POST:
            delete_id = request.POST.get('delete')
            notification_to_delete = get_object_or_404(journal_models.Notifications, pk=delete_id)
            notification_to_delete.delete()
            return redirect(reverse('core_manager_notifications'))

        if notification:
            form = forms.NotificationForm(request.POST, instance=notification)
        else:
            form = forms.NotificationForm(request.POST)

        if form.is_valid():
            save_notification = form.save(commit=False)
            save_notification.journal = request.journal
            save_notification.save()

            return redirect(reverse('core_manager_notifications'))

    template = 'core/manager/notifications/manage_notifications.html'
    context = {
        'notifications': notifications,
        'notification': notification,
        'form': form,
    }

    return render(request, template, context)


@staff_member_required
def email_templates(request):
    template_list = models.Setting.objects.filter(group__name='email')

    template = 'core/manager/email/email_templates.html'
    context = {
        'template_list': template_list,
    }

    return render(request, template, context)


@staff_member_required
def edit_email_template(request, template_code, subject=False):
    if subject:
        template_value = setting_handler.get_setting('email_subject', 'subject_{0}'.format(template_code),
                                                     request.journal, create=True)
    else:
        template_value = setting_handler.get_setting('email', template_code, request.journal, create=True)

    if template_value.setting.types == 'rich-text':
        template_value.value = linebreaksbr(template_value.value)

    edit_form = forms.EditKey(key_type=template_value.setting.types, value=template_value.value)

    if request.POST:
        value = request.POST.get('value')
        template_value.value = value
        template_value.save()

        cache.clear()

        return redirect(reverse('core_email_templates'))

    template = 'core/manager/email/edit_email_template.html'
    context = {
        'template_value': template_value,
        'edit_form': edit_form,
        'setting': template_value.setting,
    }

    return render(request, template, context)


@editor_user_required
def sections(request, section_id=None):
    section = get_object_or_404(submission_models.Section, pk=section_id,
                                journal=request.journal) if section_id else None
    sections = submission_models.Section.objects.language().fallbacks('en').filter(journal=request.journal)

    if section:
        form = forms.SectionForm(instance=section, request=request)
    else:
        form = forms.SectionForm(request=request)

    if request.POST:

        if 'delete' in request.POST:
            id = request.POST.get('delete')
            object = get_object_or_404(submission_models.Section, pk=id)
            object.delete()
        else:
            if section:
                form = forms.SectionForm(request.POST, instance=section, request=request)
            else:
                form = forms.SectionForm(request.POST, request=request)

            if form.is_valid():
                form_section = form.save(commit=False)
                form_section.journal = request.journal
                form_section.save()
                form.save_m2m()

        return redirect(reverse('core_manager_sections'))

    template = 'core/manager/sections/sections.html'
    context = {
        'sections': sections,
        'section': section,
        'form': form,
    }

    return render(request, template, context)


@editor_user_required
def pinned_articles(request):
    """
    Allows an Editor to pin articles to the top of the article page.
    """
    pinned_articles = journal_models.PinnedArticle.objects.filter(journal=request.journal)
    published_articles = logic.get_unpinned_articles(request, pinned_articles)

    if request.POST:
        if 'pin' in request.POST:
            article_id = request.POST.get('pin')
            article = get_object_or_404(submission_models.Article, pk=article_id, journal=request.journal)
            journal_models.PinnedArticle.objects.create(
                article=article,
                journal=request.journal,
                sequence=request.journal.next_pa_seq())
            messages.add_message(request, messages.INFO, 'Article pinned.')

        if 'unpin' in request.POST:
            article_id = request.POST.get('unpin')
            pinned_article = get_object_or_404(journal_models.PinnedArticle, journal=request.journal, pk=article_id)
            pinned_article.delete()
            messages.add_message(request, messages.INFO, 'Article unpinned.')

        if 'orders[]' in request.POST:
            logic.order_pinned_articles(request, pinned_articles)

        return redirect(reverse('core_pinned_articles'))

    template = 'core/manager/pinned_articles.html'
    context = {
        'pinned_articles': pinned_articles,
        'published_articles': published_articles,
    }

    return render(request, template, context)


@staff_member_required
def journal_workflow(request):
    """
    Allows a staff member to setup workflows.
    :param request: django request object
    :return: template contextualised
    """
    workflow, created = models.Workflow.objects.get_or_create(journal=request.journal)
    available_elements = logic.get_available_elements(workflow)

    if request.POST:
        if 'element_name' in request.POST:
            element_name = request.POST.get('element_name')
            element = logic.handle_element_post(workflow, element_name, request)
            if element:
                workflow.elements.add(element)
                messages.add_message(request, messages.SUCCESS, 'Element added.')
            else:
                messages.add_message(request, messages.WARNING, 'Element not found.')

        if 'delete' in request.POST:
            delete_id = request.POST.get('delete')
            delete_element = get_object_or_404(models.WorkflowElement, journal=request.journal, pk=delete_id)
            workflow.elements.remove(delete_element)
            messages.add_message(request, messages.SUCCESS, 'Removed element.')
        return redirect(reverse('core_journal_workflow'))

    template = 'core/workflow.html'
    context = {
        'workflow': workflow,
        'available_elements': available_elements,
    }

    return render(request, template, context)


@staff_member_required
def order_workflow_elements(request):
    """
    Orders workflow elements based on their position in a list group.
    :param request: django request object
    :return: an http reponse
    """
    workflow = models.Workflow.objects.get(journal=request.journal)

    if request.POST:
        ids = [int(_id) for _id in request.POST.getlist('element[]')]

        for element in workflow.elements.all():
            order = ids.index(element.pk)
            element.order = order
            element.save()

    return HttpResponse('Thanks')
