# -*- coding: utf-8 -*-
from urllib import urlencode

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.cache import never_cache

import kuma.wiki.content
from kuma.attachments.forms import AttachmentRevisionForm
from kuma.core.decorators import block_user_agents, login_required
from kuma.core.i18n import get_language_mapping
from kuma.core.urlresolvers import reverse
from kuma.core.utils import get_object_or_none, smart_int, urlparams

from .utils import document_form_initial, split_slug
from ..decorators import (check_readonly, prevent_indexing,
                          process_document_path)
from ..forms import DocumentForm, RevisionForm
from ..models import Document, Revision


@never_cache
@block_user_agents
@login_required
@process_document_path
def select_locale(request, document_slug, document_locale):
    """
    Select a locale to translate the document to.
    """
    doc = get_object_or_404(Document,
                            locale=document_locale,
                            slug=document_slug)
    return render(request, 'wiki/select_locale.html', {'document': doc})


@never_cache
@block_user_agents
@login_required
@process_document_path
@check_readonly
@prevent_indexing
def translate(request, document_slug, document_locale):
    """
    Create a new translation of a wiki document.

    * document_slug is for the default locale
    * translation is to the request locale
    """
    # TODO: Refactor this view into two views? (new, edit)
    # That might help reduce the headache-inducing branchiness.

    # The parent document to translate from
    parent_doc = get_object_or_404(Document,
                                   locale=settings.WIKI_DEFAULT_LANGUAGE,
                                   slug=document_slug)

    # HACK: Seems weird, but sticking the translate-to locale in a query
    # param is the best way to avoid the MindTouch-legacy locale
    # redirection logic.
    document_locale = request.GET.get('tolocale', document_locale)

    # Set a "Discard Changes" page
    discard_href = ''

    if settings.WIKI_DEFAULT_LANGUAGE == document_locale:
        # Don't translate to the default language.
        return redirect(reverse(
            'wiki.edit', locale=settings.WIKI_DEFAULT_LANGUAGE,
            args=[parent_doc.slug]))

    if not parent_doc.is_localizable:
        message = _(u'You cannot translate this document.')
        context = {'message': message}
        return render(request, 'handlers/400.html', context, status=400)

    based_on_rev = parent_doc.current_or_latest_revision()

    disclose_description = bool(request.GET.get('opendescription'))

    try:
        doc = parent_doc.translations.get(locale=document_locale)
        slug_dict = split_slug(doc.slug)
    except Document.DoesNotExist:
        doc = None
        disclose_description = True
        slug_dict = split_slug(document_slug)

        # Find the "real" parent topic, which is its translation
        if parent_doc.parent_topic:
            try:
                parent_topic_translated_doc = (parent_doc.parent_topic
                                                         .translations
                                                         .get(locale=document_locale))
                slug_dict = split_slug(parent_topic_translated_doc.slug +
                                       '/' +
                                       slug_dict['specific'])
            except ObjectDoesNotExist:
                pass

    user_has_doc_perm = (not doc) or (doc and doc.allows_editing_by(request.user))

    doc_form = None
    if user_has_doc_perm:
        if doc:
            # If there's an existing doc, populate form from it.
            discard_href = doc.get_absolute_url()
            doc.slug = slug_dict['specific']
            doc_initial = document_form_initial(doc)
        else:
            # If no existing doc, bring over the original title and slug.
            discard_href = parent_doc.get_absolute_url()
            doc_initial = {'title': based_on_rev.title,
                           'slug': slug_dict['specific']}
        doc_form = DocumentForm(initial=doc_initial,
                                parent_slug=slug_dict['parent'])

    initial = {
        'based_on': based_on_rev.id,
        'current_rev': doc.current_or_latest_revision().id if doc else None,
        'comment': '',
        'toc_depth': based_on_rev.toc_depth,
        'localization_tags': ['inprogress'],
    }
    content = None
    if not doc:
        content = based_on_rev.content
    if content:
        initial.update(content=kuma.wiki.content.parse(content)
                                                .filterEditorSafety()
                                                .serialize())
    instance = doc and doc.current_or_latest_revision()
    rev_form = RevisionForm(request=request,
                            instance=instance,
                            initial=initial,
                            parent_slug=slug_dict['parent'])

    if request.method == 'POST':
        which_form = request.POST.get('form-type', 'both')
        doc_form_invalid = False

        # Grab the posted slug value in case it's invalid
        posted_slug = request.POST.get('slug', slug_dict['specific'])

        if user_has_doc_perm and which_form in ['doc', 'both']:
            disclose_description = True
            post_data = request.POST.copy()

            post_data.update({'locale': document_locale})

            doc_form = DocumentForm(post_data, instance=doc,
                                    parent_slug=slug_dict['parent'])
            doc_form.instance.locale = document_locale
            doc_form.instance.parent = parent_doc

            if which_form == 'both':
                # Sending a new copy of post so the slug change above
                # doesn't cause problems during validation
                rev_form = RevisionForm(request=request,
                                        data=post_data,
                                        parent_slug=slug_dict['parent'])

            # If we are submitting the whole form, we need to check that
            # the Revision is valid before saving the Document.
            if doc_form.is_valid() and (which_form == 'doc' or
                                        rev_form.is_valid()):
                doc = doc_form.save(parent=parent_doc)

                if which_form == 'doc':
                    url = urlparams(doc.get_edit_url(), opendescription=1)
                    return redirect(url)
            else:
                doc_form.data['slug'] = posted_slug
                doc_form_invalid = True

        if doc and which_form in ['rev', 'both']:
            post_data = request.POST.copy()
            if 'slug' not in post_data:
                post_data['slug'] = posted_slug

            # update the post data with the toc_depth of original
            post_data['toc_depth'] = based_on_rev.toc_depth

            # Pass in the locale for the akistmet "blog_lang".
            post_data['locale'] = document_locale

            rev_form = RevisionForm(request=request,
                                    data=post_data,
                                    parent_slug=slug_dict['parent'])
            rev_form.instance.document = doc  # for rev_form.clean()

            if rev_form.is_valid() and not doc_form_invalid:
                parent_id = request.POST.get('parent_id', '')

                # Attempt to set a parent
                if parent_id:
                    try:
                        parent_doc = get_object_or_404(Document, id=parent_id)
                        rev_form.instance.document.parent = parent_doc
                        doc.parent = parent_doc
                        rev_form.instance.based_on.document = doc.original
                    except Document.DoesNotExist:
                        pass

                rev_form.save(doc)
                # If this is an Ajax POST, then return a JsonResponse
                if request.is_ajax():
                    data = {
                        'error': False,
                        'new_revision_id': rev_form.instance.id,
                    }

                    return JsonResponse(data)

                # Construct the redirect URL, adding any needed parameters
                url = doc.get_absolute_url()
                params = {}
                # Parameter for the document saved, so that we can delete the cached draft on load
                params['rev_saved'] = request.POST.get('current_rev', '')
                url = '%s?%s' % (url, urlencode(params))
                return redirect(url)
            else:
                # If this is an Ajax POST, then return a JsonResponse with error
                if request.is_ajax():
                    if 'current_rev' in rev_form._errors:
                        # Make the error message safe so the '<' and '>' don't
                        # get turned into '&lt;' and '&gt;', respectively
                        rev_form.errors['current_rev'][0] = mark_safe(
                            rev_form.errors['current_rev'][0])
                    errors = [rev_form.errors[key][0] for key in rev_form.errors.keys()]
                    data = {
                        "error": True,
                        "error_message": errors,
                        "new_revision_id": rev_form.instance.id,
                    }
                    return JsonResponse(data=data)

    if doc:
        from_id = smart_int(request.GET.get('from'), None)
        to_id = smart_int(request.GET.get('to'), None)

        revision_from = get_object_or_none(Revision,
                                           pk=from_id,
                                           document=doc.parent)
        revision_to = get_object_or_none(Revision,
                                         pk=to_id,
                                         document=doc.parent)
    else:
        revision_from = revision_to = None

    parent_split = split_slug(parent_doc.slug)

    language_mapping = get_language_mapping()
    language = language_mapping[document_locale.lower()]
    default_locale = language_mapping[settings.WIKI_DEFAULT_LANGUAGE.lower()]

    context = {
        'parent': parent_doc,
        'document': doc,
        'document_form': doc_form,
        'revision_form': rev_form,
        'locale': document_locale,
        'default_locale': default_locale,
        'language': language,
        'based_on': based_on_rev,
        'disclose_description': disclose_description,
        'discard_href': discard_href,
        'attachment_form': AttachmentRevisionForm(),
        'specific_slug': parent_split['specific'],
        'parent_slug': parent_split['parent'],
        'revision_from': revision_from,
        'revision_to': revision_to,
    }
    return render(request, 'wiki/translate.html', context)
