from django.contrib.auth import authenticate
from django.core.urlresolvers import reverse
from django.core.validators import RegexValidator
from django.db.models import Count
from django import forms
from django.shortcuts import redirect
from django.utils.functional import cached_property
from django.contrib.auth.forms import AuthenticationForm as BaseAuthenticationForm
from django.contrib.auth import login
from django.views.generic import TemplateView
from django.utils.translation import ugettext_lazy as _
from django.conf import settings
from pretix.base.models import User

from pretix.presale.views import EventViewMixin, CartDisplayMixin


class EventIndex(EventViewMixin, CartDisplayMixin, TemplateView):
    template_name = "pretixpresale/event/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Fetch all items
        items = self.request.event.items.all().select_related(
            'category',  # for re-grouping
        ).prefetch_related(
            'properties',  # for .get_all_available_variations()
            'quotas', 'variations__quotas'  # for .availability()
        ).annotate(quotac=Count('quotas')).filter(
            quotac__gt=0
        ).order_by('category__position', 'category_id', 'name')

        for item in items:
            item.available_variations = sorted(item.get_all_available_variations(),
                                               key=lambda vd: vd.ordered_values())
            item.has_variations = (len(item.available_variations) != 1
                                   or not item.available_variations[0].empty())
            if not item.has_variations:
                item.cached_availability = list(item.check_quotas())
                item.cached_availability[1] = min(item.cached_availability[1],
                                                  self.request.event.max_items_per_order)
                item.price = item.available_variations[0]['price']
            else:
                for var in item.available_variations:
                    var.cached_availability = list(var['variation'].check_quotas())
                    var.cached_availability[1] = min(var.cached_availability[1],
                                                     self.request.event.max_items_per_order)

        # Regroup those by category
        context['items_by_category'] = sorted([
            # a group is a tuple of a category and a list of items
            (cat, [i for i in items if i.category_id == cat.identity])
            for cat in set([i.category for i in items])  # insert categories into a set for uniqueness
        ], key=lambda group: (group[0].position, group[0].pk))  # a set is unsorted, so sort again by category

        context['cart'] = self.get_cart() if self.request.user.is_authenticated() else None
        return context


class LoginForm(BaseAuthenticationForm):
    username = forms.CharField(
        label=_('Username'),
        help_text=_('If you registered for multiple events, your username is your email address.')
    )
    password = forms.CharField(
        label=_('Password'),
        widget=forms.PasswordInput
    )

    error_messages = {
        'invalid_login': _("Please enter a correct username and password."),
        'inactive': _("This account is inactive."),
    }

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super(forms.Form, self).__init__(*args, **kwargs)

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username and password:
            if '@' in username:
                identifier = username.lower()
            else:
                identifier = "%s@%s.event.pretix" % (username, self.request.event.identity)
            self.user_cache = authenticate(identifier=identifier,
                                           password=password)
            if self.user_cache is None:
                raise forms.ValidationError(
                    self.error_messages['invalid_login'],
                    code='invalid_login',
                )
            else:
                self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data


class GlobalRegistrationForm(forms.Form):
    error_messages = {
        'duplicate_email': _("You already registered with that e-mail address, please use the login form."),
        'pw_mismatch': _("Please enter the same password twice"),
    }
    email = forms.EmailField(
        label=_('Email address'),
        required=True
    )
    password = forms.CharField(
        label=_('Password'),
        widget=forms.PasswordInput,
        required=True
    )
    password_repeat = forms.CharField(
        label=_('Repeat password'),
        widget=forms.PasswordInput
    )

    def clean(self):
        password1 = self.cleaned_data.get('password')
        password2 = self.cleaned_data.get('password_repeat')

        if password1 and password1 != password2:
            raise forms.ValidationError(
                self.error_messages['pw_mismatch'],
                code='pw_mismatch',
            )

        return self.cleaned_data

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(identifier=email).exists():
            raise forms.ValidationError(
                self.error_messages['duplicate_email'],
                code='duplicate_email',
            )
        return email


class LocalRegistrationForm(forms.Form):
    error_messages = {
        'invalid_username': _("Please only use characters, numbers or ./+/-/_ in your username."),
        'duplicate_username': _("This username is already taken. Please choose a different one."),
        'pw_mismatch': _("Please enter the same password twice"),
    }
    username = forms.CharField(
        label=_('Username'),
        validators=[
            RegexValidator(
                regex='^[a-zA-Z0-9\.+\-_]*$',
                code='invalid_username',
                message=error_messages['invalid_username']
            ),
        ],
        required=True
    )
    email = forms.EmailField(
        label=_('E-mail address'),
        required=False
    )
    password = forms.CharField(
        label=_('Password'),
        widget=forms.PasswordInput,
        required=True
    )
    password_repeat = forms.CharField(
        label=_('Repeat password'),
        widget=forms.PasswordInput
    )

    def __init__(self, request, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.fields['email'].required = (request.event.settings.user_mail_required == 'True')

    def clean(self):
        password1 = self.cleaned_data.get('password')
        password2 = self.cleaned_data.get('password_repeat')

        if password1 and password1 != password2:
            raise forms.ValidationError(
                self.error_messages['pw_mismatch'],
                code='pw_mismatch',
            )

        return self.cleaned_data

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(event=self.request.event, username=username).exists():
            raise forms.ValidationError(
                self.error_messages['duplicate_username'],
                code='duplicate_username',
            )
        return username


class EventLogin(EventViewMixin, TemplateView):
    template_name = 'pretixpresale/event/login.html'

    def redirect_to_next(self):
        if 'next' in self.request.GET:
            return redirect(self.request.GET.get('next'))
        else:
            return redirect(reverse(
                'presale:event.index', kwargs={
                    'organizer': self.request.event.organizer.slug,
                    'event': self.request.event.slug,
                }
            ))

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated() and \
                (request.user.event is None or request.user.event == request.event):
            return self.redirect_to_next()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if request.POST.get('form') == 'login':
            form = self.login_form
            if form.is_valid() and form.user_cache:
                login(request, form.user_cache)
                return self.redirect_to_next()
        elif request.POST.get('form') == 'local_registration':
            form = self.local_registration_form
            if form.is_valid():
                user = User.objects.create_local_user(
                    request.event, form.cleaned_data['username'], form.cleaned_data['password'],
                    email=form.cleaned_data['email'] if form.cleaned_data['email'] != '' else None
                )
                user = authenticate(identifier=user.identifier, password=form.cleaned_data['password'])
                login(request, user)
                return self.redirect_to_next()
        elif request.POST.get('form') == 'global_registration':
            form = self.global_registration_form
            if form.is_valid():
                user = User.objects.create_global_user(
                    form.cleaned_data['email'], form.cleaned_data['password'],
                )
                user = authenticate(identifier=user.identifier, password=form.cleaned_data['password'])
                login(request, user)
                return self.redirect_to_next()
        return super().get(request, *args, **kwargs)

    @cached_property
    def login_form(self):
        return LoginForm(
            self.request,
            data=self.request.POST if self.request.POST.get('form', '') == 'login' else None
        )

    @cached_property
    def global_registration_form(self):
        if settings.PRETIX_GLOBAL_REGISTRATION:
            return GlobalRegistrationForm(
                data=self.request.POST if self.request.POST.get('form', '') == 'global_registration' else None
            )
        else:
            return None

    @cached_property
    def local_registration_form(self):
        return LocalRegistrationForm(
            self.request,
            data=self.request.POST if self.request.POST.get('form', '') == 'local_registration' else None
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['login_form'] = self.login_form
        context['global_registration_form'] = self.global_registration_form
        context['local_registration_form'] = self.local_registration_form
        return context
