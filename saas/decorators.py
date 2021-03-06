# Copyright (c) 2017, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
The access control logic is best configured in the site URLConf through
extensions like `django-urldecorators`_. This is not only more flexible but
also make security audits a lot easier.

.. _django-urldecorators: https://github.com/mila/django-urldecorators
"""

import logging

from functools import wraps
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.shortcuts import get_object_or_404
from django.utils.decorators import available_attrs
from django.utils import six

from . import settings
from .models import (Charge, Organization, Plan, Signature, Subscription,
    get_broker)
from .utils import datetime_or_now, get_role_model


LOGGER = logging.getLogger(__name__)

# With ``WEAK`` authorization, contributors can issue POST, PUT, PATCH, DELETE
# requests. With ``NORMAL`` authorization, contributors can issue GET requests
# and nothing else. With ``STRONG`` authorization, contributors cannot issue
# any requests, only managers can.
WEAK = 0
NORMAL = 1
STRONG = 2


def _valid_role(user, candidates, role):
    """
    Returns the subset of a set of ``Organization`` *candidates*
    which have *user* listed with a role.
    """
    results = []
    if settings.BYPASS_PERMISSION_CHECK:
        if user:
            username = user.username
        else:
            username = '(none)'
        LOGGER.warning("Skip permission check for %s on organizations %s",
                       username, candidates)
        return candidates
    if role is not None and user and user.is_authenticated():
        if isinstance(role, (list, tuple)):
            kwargs = {'role_description__slug__in': role}
        else:
            kwargs = {'role_description__slug': role}
        results = Organization.objects.filter(
            pk__in=get_role_model().objects.valid_for(
                organization__in=candidates, user=user, **kwargs).values(
                'organization')).values('slug')
    return results


def _valid_manager(user, candidates):
    """
    Returns the subset of a queryset of ``Organization``, *candidates*
    which have *user* as a manager.
    """
    return _valid_role(user, candidates, settings.MANAGER)


def _filter_valid_access(request, candidates,
                         strength=NORMAL, roledescription=None):
    """
    Returns a tuple made of two lists of ``Organization`` from *candidates*.

    The first item in the tuple are the organizations managed by *request.user*
    while the second item in the tuple are the organizations for which
    *request.user* has a *roledescription* role (ex: contributors).

    The set of contributed organizations is further filtered by
    *request.method* and *strength*.
    """
    if roledescription is None:
        roledescription = settings.CONTRIBUTOR
    managed = []
    contributed = []
    managed = _valid_manager(request.user, candidates)
    if request.method == "GET":
        if strength != STRONG:
            contributed = _valid_role(request.user, candidates, roledescription)
    else:
        if strength == WEAK:
            contributed = _valid_role(request.user, candidates, roledescription)
    return managed, contributed


def _has_valid_access(request, candidates,
                      strength=NORMAL, roledescription=None):
    """
    Returns True if any candidate is accessible to the request user.
    """
    managed, contributed = _filter_valid_access(request, candidates,
        strength=strength, roledescription=roledescription)
    return len(managed) + len(contributed) > 0


def _insert_url(request, redirect_field_name=REDIRECT_FIELD_NAME,
                inserted_url=None):
    '''Redirects to the *inserted_url* before going to the orginal
    request path.'''
    # This code is pretty much straightforward
    # from contrib.auth.user_passes_test
    path = request.build_absolute_uri()
    # If the login url is the same scheme and net location then just
    # use the path as the "next" url.
    login_scheme, login_netloc = six.moves.urllib.parse.urlparse(
        inserted_url)[:2]
    current_scheme, current_netloc = six.moves.urllib.parse.urlparse(path)[:2]
    if ((not login_scheme or login_scheme == current_scheme) and
        (not login_netloc or login_netloc == current_netloc)):
        path = request.get_full_path()
    # As long as *inserted_url* is not None, this call will redirect
    # anything (i.e. inserted_url), not just the login.
    from django.contrib.auth.views import redirect_to_login
    return redirect_to_login(path, inserted_url, redirect_field_name)


def _get_accept_list(request):
    http_accept = request.META.get('HTTP_ACCEPT', '*/*')
    return [item.strip() for item in http_accept.split(',')]


def fail_authenticated(request):
    """
    Authenticated
    """
    if not request.user.is_authenticated():
        return reverse(settings.LOGIN_URL)
    return False


def fail_agreement(request, agreement=settings.TERMS_OF_USE):
    """
    Agreed to %(saas.Agreement)s
    """
    if not Signature.objects.has_been_accepted(
            agreement=agreement, user=request.user):
        return reverse('legal_sign_agreement', kwargs={'agreement': agreement})
    return False


def fail_subscription(request, organization=None, plan=None):
    """
    Subscribed or was subscribed to %(saas.Plan)s
    """
    if _has_valid_access(request, [get_broker()]):
        # Bypass if a manager for the broker.
        return False
    if organization and not isinstance(organization, Organization):
        organization = get_object_or_404(Organization, slug=organization)
    subscriptions = Subscription.objects.valid_for(
        organization=organization).order_by("ends_at")
    # ``order_by("ends_at")`` will get the subscription that ends the earliest,
    # yet is greater than Today (``subscribed_at``).
    if plan:
        if not isinstance(plan, Plan):
            plan = get_object_or_404(Plan, slug=plan)
        subscriptions = subscriptions.filter(plan=plan)
        active_subscription = subscriptions.first()
        if active_subscription is None:
            return "%s?plan=%s" % (
                reverse('saas_organization_cart', args=(organization,)), plan)
    else:
        active_subscription = subscriptions.first()
        if active_subscription is None:
            return reverse('saas_cart_plan_list')
        plan = active_subscription.plan
    if active_subscription.is_locked:
        return reverse('saas_organization_balance', args=(organization, plan))
    return False


def fail_paid_subscription(request, organization=None, plan=None):
    """
    Subscribed to %(saas.Plan)s
    """
    subscribed_at = datetime_or_now()
    if _has_valid_access(request, [get_broker()]):
        # Bypass if a manager for the broker.
        return False
    if organization and not isinstance(organization, Organization):
        organization = get_object_or_404(Organization, slug=organization)
    subscriptions = organization.get_active_subscriptions(
        at_time=subscribed_at).order_by('ends_at')
    # ``order_by("ends_at")`` will get the subscription that ends the earliest,
    # yet is greater than Today (``subscribed_at``).
    if plan:
        if not isinstance(plan, Plan):
            plan = get_object_or_404(Plan, slug=plan)
        subscriptions = subscriptions.filter(plan=plan)
        active_subscription = subscriptions.first()
        if active_subscription is None:
            return "%s?plan=%s" % (
                reverse('saas_organization_cart', args=(organization,)), plan)
    else:
        active_subscription = subscriptions.first()
        if active_subscription is None:
            return reverse('saas_cart_plan_list')
        plan = active_subscription.plan
    if active_subscription.is_locked:
        return reverse('saas_organization_balance', args=(organization, plan))
    return False


def _fail_direct(request, organization=None, roledescription=None,
                 strength=NORMAL):
    if isinstance(organization, Charge):
        # implicit natural conversion
        organization = organization.customer
    elif organization and not isinstance(organization, Organization):
        try:
            organization = Organization.objects.get(slug=organization)
        except Organization.DoesNotExist:
            charge = get_object_or_404(Charge, processor_key=organization)
            organization = charge.customer
    else:
        organization = get_broker()
    result = not(organization and _has_valid_access(
        request, [organization],
        strength=strength, roledescription=roledescription))
    return result


def fail_direct(request, organization=None, roledescription=None):
    """
    Direct %(saas.RoleDescription)s for :organization restricted to GET

    Returns False if the authenticated ``request.user`` is a direct
    ``roledescription`` (ex: contributor) or manager for ``organization``
    and the user's role allows for ``request.method``.

    Managers can issue all types of requests (GET, POST, etc.) while
    ``request.user`` with a ``roledescription`` (ex: contributors)
    are restricted to GET requests.
    """
    return _fail_direct(request, organization=organization,
        strength=NORMAL, roledescription=roledescription)


def fail_direct_weak(request, organization=None, roledescription=None):
    """
    Direct %(saas.RoleDescription)s for :organization

    Returns False if the authenticated ``request.user`` is a direct
    ``roledescription`` (ex: contributor) or manager for ``organization``.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).

    .. image:: perms-contrib.*
    """
    return _fail_direct(request, organization=organization,
        strength=WEAK, roledescription=roledescription)


def fail_direct_strong(request, organization=None):
    """
    Direct Managers for :organization
    """
    return _fail_direct(request, organization=organization, strength=STRONG)


def _fail_provider(request, organization=None,
                   strength=NORMAL, roledescription=None):
    if isinstance(organization, Charge):
        # implicit natural conversion
        organization = organization.customer
    elif organization and not isinstance(organization, Organization):
        try:
            organization = Organization.objects.get(slug=organization)
        except Organization.DoesNotExist:
            charge = get_object_or_404(Charge, processor_key=organization)
            organization = charge.customer
    candidates = [get_broker()]
    if organization:
        candidates = ([organization]
            + list(Organization.objects.providers_to(organization))
            + candidates)
    return not _has_valid_access(request, candidates,
        strength=strength, roledescription=roledescription)


def fail_provider(request, organization=None, roledescription=None):
    #pylint:disable=line-too-long
    """
    Provider or Direct %(saas.RoleDescription)s for :organization restricted to GET

    Returns False if the authenticated ``request.user`` is a direct
    ``roledescription`` (ex: contributor) or manager for ``organization``,
    or to a provider of ``organization``, and the user's role allows for
    ``request.method``.

    Managers can issue all types of requests (GET, POST, etc.) while
    the ``request.user`` with another role (ex: contributor) are restricted
    to GET requests.
    """
    return _fail_provider(request, organization=organization,
        strength=NORMAL, roledescription=roledescription)


def fail_provider_weak(request, organization=None, roledescription=None):
    """
    Provider or Direct %(saas.RoleDescription)s for :organization

    Returns False if the authenticated ``request.user`` is a direct
    ``roledescription`` (ex: contributor) or manager for ``organization``
    or to a provider of ``organization``.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).

    .. image:: perms-contrib-subscribes.*
    """
    return _fail_provider(request, organization=organization,
        strength=WEAK, roledescription=roledescription)


def fail_provider_strong(request, organization=None):
    """
    Provider or Direct Managers for :organization
    """
    return _fail_provider(request, organization=organization, strength=STRONG)


def _fail_provider_only(request, organization=None, strength=NORMAL,
                        roledescription=None):
    if isinstance(organization, Charge):
        # implicit natural conversion
        organization = organization.customer
    elif organization and not isinstance(organization, Organization):
        try:
            organization = Organization.objects.get(slug=organization)
        except Organization.DoesNotExist:
            charge = get_object_or_404(Charge, processor_key=organization)
            organization = charge.customer
    candidates = [get_broker()]
    if organization:
        candidates = (list(Organization.objects.providers_to(organization))
            + candidates)
    return not _has_valid_access(request, candidates,
        strength=strength, roledescription=roledescription)


def fail_provider_only(request, organization=None, roledescription=None):
    """
    Provider %(saas.RoleDescription)s for :organization restricted to GET

    Returns False if the request authenticated ``User``
    is a contributor (or manager) for a provider to the ``Organization``
    associated to the request.

    Both managers and contributors can issue all types of requests
    (GET, POST, etc.).
    """
    return _fail_provider_only(
        request, organization=organization,
        strength=NORMAL, roledescription=roledescription)


def fail_provider_only_weak(request, organization=None, roledescription=None):
    """
    Provider %(saas.RoleDescription)s for :organization

    Returns False if the authenticated ``request.user`` is a ``roledescription``
    (ex: contributor) or manager for a provider of ``organization``.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).

    .. image:: perms-contrib-provider-only.*
    """
    return _fail_provider_only(
        request, organization=organization,
        strength=NORMAL, roledescription=roledescription)


def fail_provider_only_strong(request, organization=None):
    """
    Provider Managers for :organization
    """
    return _fail_provider_only(
        request, organization=organization, strength=STRONG)


def _fail_self_provider(request, user=None, strength=NORMAL,
                        roledescription=None):
    if request.user.username != user:
        # Organization that are managed by both users
        directs = Organization.objects.accessible_by(user)
        providers = Organization.objects.providers(
            Subscription.objects.valid_for(organization__in=directs))
        candidates = list(directs) + list(providers) + [get_broker()]
        return not _has_valid_access(request, candidates,
            strength=strength, roledescription=roledescription)
    return False


def fail_self_provider(request, user=None, roledescription=None):
    """
    Self or %(saas.RoleDescription)s Associated to :user restricted to GET

    Returns False if the authenticated ``request.user`` is the ``user``
    passed as an argument and the request.user's role allows for
    ``request.method``.
    Returns False also if the authenticated user is a ``roledescription``
    (ex: contributor) or manager for any organizations associated to
    ``user``, and provider of such organizations  and the request.user's role
    allows for ``request.method``.

    Self and managers can issue all types of requests (GET, POST, etc.) while
    a ``request.user`` with a different role (ex: contributor) is restricted
    to GET requests.
    """
    return _fail_self_provider(request, user=user,
        strength=NORMAL, roledescription=roledescription)


def fail_self_provider_weak(request, user=None, roledescription=None):
    """
    Self or %(saas.RoleDescription)s Associated to :user

    Returns False if the authenticated ``request.user`` is the ``user``
    passed as an argument.
    Returns False also if the authenticated user is a ``roledescription``
    (ex: contributor) or manager for any organizations associated to
    ``user``, and provider of such organizations.

    All self, ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).

    .. image:: perms-self-contrib-subscribes.*
    """
    return _fail_self_provider(request, user=user,
        strength=WEAK, roledescription=roledescription)


def fail_self_provider_strong(request, user=None):
    """
    Self or Managers Associated to :user
    """
    return _fail_self_provider(request, user=user, strength=STRONG)


def redirect_or_denied(request, inserted_url,
                       redirect_field_name=REDIRECT_FIELD_NAME, descr=None):
    http_accepts = _get_accept_list(request)
    if ('text/html' in http_accepts
        and isinstance(inserted_url, six.string_types)):
        return _insert_url(request, redirect_field_name=redirect_field_name,
                           inserted_url=inserted_url)
    if descr is None:
        descr = ""
    raise PermissionDenied(descr)


def requires_authenticated(function=None,
                           redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the user is authenticated.

    ``django.contrib.auth.decorators.login_required`` will automatically
    redirect to the login page. We want to raise a ``PermissionDenied``
    instead when Content-Type is showing we are dealing with an API request.
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            redirect_url = fail_authenticated(request)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_agreement(function=None,
                       agreement=settings.TERMS_OF_USE,
                       redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the user has signed a particular
    legal agreement, redirecting to the agreement signature or log-in page
    if necessary.
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            redirect_url = fail_agreement(request, agreement=agreement)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_subscription(function=None,
                organization_kwarg_slug='organization',
                plan_kwarg_slug='subscribed_plan',
                redirect_field_name=REDIRECT_FIELD_NAME,
                strength=NORMAL,
                roledescription=None):
    #pylint:disable=too-many-arguments
    """
    Decorator that checks an organization is or was subscribed to a plan.
    It redirects to an appropriate page when this is not the case:

    - Checkout page when there never was a subscription (to plan).
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            subscriber = get_object_or_404(
                Organization, slug=kwargs.get(organization_kwarg_slug, None))
            if _fail_provider(request, organization=subscriber,
                    strength=strength, roledescription=roledescription):
                raise PermissionDenied("%(user)s is neither a manager '\
' of %(organization)s nor a manager of one of %(organization)s providers."
                % {'user': request.user, 'organization': subscriber})
            redirect_url = fail_subscription(request,
                organization=subscriber, plan=kwargs.get(plan_kwarg_slug, None))
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_paid_subscription(function=None,
                organization_kwarg_slug='organization',
                plan_kwarg_slug='subscribed_plan',
                redirect_field_name=REDIRECT_FIELD_NAME,
                strength=NORMAL,
                roledescription=None):
    #pylint:disable=too-many-arguments
    """
    Decorator that checks a specified subscription is paid. It redirects to an
    appropriate page when this is not the case:

    - Payment page when no charge is associated to the subscription,
    - Update Credit Card page when ``charge.status`` is ``failed``,
    - Waiting page when ``charge.status`` is ``in-progress``.
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            subscriber = get_object_or_404(
                Organization, slug=kwargs.get(organization_kwarg_slug, None))
            if _fail_provider(request, organization=subscriber,
                    strength=strength, roledescription=roledescription):
                raise PermissionDenied("%(user)s is neither a manager '\
' of %(organization)s nor a manager of one of %(organization)s providers."
                % {'user': request.user, 'organization': subscriber})
            redirect_url = fail_paid_subscription(request,
                organization=subscriber, plan=kwargs.get(plan_kwarg_slug, None))
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_direct(function=None, roledescription=None,
                    redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the authenticated ``request.user``
    is a direct ``roledescription`` (ex: contributor) or manager
    for the ``Organization`` associated to the request.

    Managers can issue all types of requests (GET, POST, etc.). while
    ``roledescription`` (ex: contributors) are restricted to GET requests.

    .. image:: perms-contrib.*
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            slug = kwargs.get('charge', kwargs.get('organization', None))
            redirect_url = fail_direct(request, organization=slug,
                    roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(user)s is not a direct manager '\
' of %(organization)s." % {'user': request.user, 'organization': slug})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_direct_weak(function=None, roledescription=None,
                         redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is a direct ``roledescription`` (ex: contributor) or manager
    for the ``Organization`` associated to the request.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            slug = kwargs.get('charge', kwargs.get('organization', None))
            redirect_url = fail_direct_weak(request, organization=slug,
                    roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(user)s is not a direct manager '\
' of %(organization)s." % {'user': request.user, 'organization': slug})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_provider(function=None, roledescription=None,
                      redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is a ``roledescription`` (ex: contributor) or manager for
    the ``Organization`` associated to the request itself or
    a ``roledescription`` (or manager) to a provider for the ``Organization``
    associated to the request.

    Managers can issue all types of requests (GET, POST, etc.). while
    ``roledescription`` (ex: contributors) are restricted to GET requests.

    .. image:: perms-contrib-subscribes.*
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            charge = kwargs.get('charge', None)
            if charge is not None:
                obj = get_object_or_404(Charge, processor_key=charge)
            else:
                obj = kwargs.get('organization', None)
            redirect_url = fail_provider(request, organization=obj,
                roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s is neither a manager "\
" for %(slug)s nor a manager of one of %(slug)s providers." % {
    'auth': request.user,
    'slug': kwargs.get('charge', kwargs.get('organization', None))})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_provider_weak(function=None, roledescription=None,
                           redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is a ``roledescription`` (ex: contributor) or manager
    for the ``Organization`` associated to the request itself
    or a ``roledescription`` or manager to a provider for the ``Organization``
    associated to the request.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            charge = kwargs.get('charge', None)
            if charge is not None:
                obj = get_object_or_404(Charge, processor_key=charge)
            else:
                obj = kwargs.get('organization', None)
            redirect_url = fail_provider_weak(request, organization=obj,
                    roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s is neither a manager "\
" for %(slug)s nor a manager of one of %(slug)s providers." % {
    'auth': request.user,
    'slug': kwargs.get('charge', kwargs.get('organization', None))})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_provider_only(function=None, roledescription=None,
                           redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is a ``roledescription`` (ex: contributor) or manager for a provider
    to the ``Organization`` associated to the request.

    Managers can issue all types of requests (GET, POST, etc.). while
    ``roledescription`` (ex: contributors) are restricted to GET requests.

    .. image:: perms-contrib-provider-only.*
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            charge = kwargs.get('charge', None)
            if charge is not None:
                obj = get_object_or_404(Charge, processor_key=charge)
            else:
                obj = kwargs.get('organization', None)
            redirect_url = fail_provider_only(request, organization=obj,
                    roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s has no direct relation to"\
" a provider for %(slug)s." % {'auth': request.user,
        'slug': kwargs.get('charge', kwargs.get('organization', None))})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_provider_only_weak(function=None, roledescription=None,
                                redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is a ``roledescription`` (ex: contributor) or manager for a provider
    to the ``Organization`` associated to the request.

    Both ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            charge = kwargs.get('charge', None)
            if charge is not None:
                obj = get_object_or_404(Charge, processor_key=charge)
            else:
                obj = kwargs.get('organization', None)
            redirect_url = fail_provider_only_weak(request, organization=obj,
                    roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s has no direct relation to"\
" a provider for %(slug)s." % {'auth': request.user,
        'slug': kwargs.get('charge', kwargs.get('organization', None))})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_self_provider(function=None, roledescription=None,
                           redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is the user associated to the URL.
    Authenticated users that can also access the URL through this decorator
    are ``roledescription`` (ex: contributors) or managers for any
    ``Organization`` associated with the user served by the URL (the accessed
    user is a direct ``roledescription`` or manager of the organization) and
    transitively contributors (or managers) for any provider to one of these
    direct organizations.

    Managers can issue all types of requests (GET, POST, etc.). while
    ``roledescription`` (ex: contributors) are restricted to GET requests.

    .. image:: perms-self-contrib-subscribes.*
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            redirect_url = fail_self_provider(request,
                user=kwargs.get('user', None), roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s has neither a direct"\
" relation to an organization connected to %(user)s nor a connection to one"\
"of the providers to such organization." % {
    'auth': request.user, 'user': kwargs.get('user', None)})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator


def requires_self_provider_weak(function=None, roledescription=None,
                                redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Decorator for views that checks that the request authenticated ``User``
    is the user associated to the URL.
    Authenticated users that can also access the URL through this decorator
    are ``roledescription`` (ex: contributors) or managers for any
    ``Organization`` associated with the user served by the URL (the accessed
    user is a direct ``roledescription``
    or manager of the organization) and transitively ``roledescription``s
    or managers for any provider to one of these direct organizations.

    Self, ``roledescription`` and managers can issue all types of requests
    (GET, POST, etc.).
    """
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            redirect_url = fail_self_provider_weak(request,
                user=kwargs.get('user', None), roledescription=roledescription)
            if redirect_url:
                return redirect_or_denied(request, redirect_url,
                    redirect_field_name=redirect_field_name,
                    descr="%(auth)s has neither a direct"\
" relation to an organization connected to %(user)s nor a connection to one"\
"of the providers to such organization." % {
    'auth': request.user, 'user': kwargs.get('user', None)})
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if function:
        return decorator(function)
    return decorator
