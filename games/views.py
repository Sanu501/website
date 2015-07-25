"""Views for lutris main app"""
# pylint: disable=E1101, W0613
import json
import logging
from django.conf import settings
from django.http import HttpResponse, Http404
from django.views.generic import ListView  # , DetailView
from django.views.decorators.http import require_POST
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.core.urlresolvers import reverse
from django.core.mail import mail_managers
from django.contrib.syndication.views import Feed
from django.contrib.auth.decorators import login_required

from rest_framework import generics, filters
from sorl.thumbnail import get_thumbnail

from platforms.models import Platform
from .models import Game, Installer, GameSubmission, InstallerIssue
from .serializers import GameSerializer
from . import models
from .forms import InstallerForm, ScreenshotForm, GameForm
from .util.pagination import get_page_range

LOGGER = logging.getLogger(__name__)


class GameList(ListView):
    model = models.Game
    context_object_name = "games"
    paginate_by = 25

    def get_queryset(self):
        unpublished_filter = self.request.GET.get('unpublished-filter')
        if unpublished_filter:
            queryset = models.Game.objects.all()
        else:
            queryset = models.Game.objects.with_installer()

        statement = ''

        # Filter open source
        filters = []
        fully_libre_filter = self.request.GET.get('fully-libre-filter')
        if fully_libre_filter:
            filters.append('fully_libre')
        open_engine_filter = self.request.GET.get('open-engine-filter')
        if open_engine_filter:
            filters.append('open_engine')

        if filters:
            for flag in filters:
                statement += "Q(flags=Game.flags.%s) | " % flag
            statement = statement.strip('| ')

        # Filter free
        filters = []
        free_filter = self.request.GET.get('free-filter')
        if free_filter:
            filters.append('free')
        freetoplay_filter = self.request.GET.get('freetoplay-filter')
        if freetoplay_filter:
            filters.append('freetoplay')
        pwyw_filter = self.request.GET.get('pwyw-filter')
        if pwyw_filter:
            filters.append('pwyw')

        if filters:
            if statement:
                statement = statement + ', '
            for flag in filters:
                statement += "Q(flags=Game.flags.%s) | " % flag
            statement = statement.strip('| ')

        if statement:
            queryset = eval("queryset.filter(%s)" % statement)

        search_terms = self.request.GET.get('q')
        if search_terms:
            queryset = queryset.filter(name__icontains=search_terms)
        return queryset

    def get_pages(self, context):
        page = context['page_obj']
        paginator = page.paginator
        page_indexes = get_page_range(paginator.num_pages, page.number)
        pages = []
        for i in page_indexes:
            if i:
                pages.append(paginator.page(i))
            else:
                pages.append(None)
        return pages

    def get_context_data(self, **kwargs):
        context = super(GameList, self).get_context_data(**kwargs)
        context['page_range'] = self.get_pages(context)
        get_args = self.request.GET
        context['search_terms'] = get_args.get('q')
        context['all_open_source'] = get_args.get('all-open-source')
        context['fully_libre_filter'] = get_args.get('fully-libre-filter')
        context['open_engine_filter'] = get_args.get('open-engine-filter')
        context['all_free'] = get_args.get('all-free')
        context['free_filter'] = get_args.get('free-filter')
        context['freetoplay_filter'] = get_args.get('freetoplay-filter')
        context['pwyw_filter'] = get_args.get('pwyw-filter')
        context['unpublished_filter'] = get_args.get('unpublished-filter')
        for key in context:
            if key.endswith('_filter') and context[key]:
                context['show_advanced'] = True
                break
        context['platforms'] = Platform.objects.all()
        context['genres'] = models.Genre.objects.all()
        return context


class GameListByYear(GameList):
    def get_queryset(self):
        queryset = super(GameListByYear, self).get_queryset()
        return queryset.filter(year=self.args[0])

    def get_context_data(self, **kwargs):
        context = super(GameListByYear, self).get_context_data(**kwargs)
        context['year'] = self.args[0]
        return context


class GameListByGenre(GameList):
    """View for games filtered by genre"""
    def get_queryset(self):
        queryset = super(GameListByGenre, self).get_queryset()
        return queryset.filter(genres__slug=self.args[0])

    def get_context_data(self, **kwargs):
        context = super(GameListByGenre, self).get_context_data(**kwargs)
        try:
            context['genre'] = models.Genre.objects.get(slug=self.args[0])
        except models.Genre.DoesNotExist:
            raise Http404
        return context


class GameListByCompany(GameList):
    """View for games filtered by publisher"""
    def get_queryset(self):
        queryset = super(GameListByCompany, self).get_queryset()
        return queryset.filter(Q(publisher__slug=self.args[0])
                               | Q(developer__slug=self.args[0]))

    def get_context_data(self, **kwargs):
        context = super(GameListByCompany, self).get_context_data(**kwargs)
        try:
            context['company'] = models.Company.objects.get(slug=self.args[0])
        except models.Company.DoesNotExist:
            raise Http404
        return context


class GameListByPlatform(GameList):
    """View for games filtered by platform"""
    def get_queryset(self):
        queryset = super(GameListByPlatform, self).get_queryset()
        return queryset.filter(platforms__slug=self.kwargs['slug'])

    def get_context_data(self, **kwargs):
        context = super(GameListByPlatform, self).get_context_data(**kwargs)
        try:
            context['platform'] = Platform.objects.get(
                slug=self.kwargs['slug']
            )
        except Platform.DoesNotExist:
            raise Http404
        return context


class GameListView(generics.ListAPIView):
    serializer_class = GameSerializer
    filter_backends = (filters.SearchFilter, )
    search_fields = ('slug', )

    def get_queryset(self):
        if 'games' in self.request.GET:
            game_slugs = self.request.GET.getlist('games')
        elif 'games' in self.request.data:
            game_slugs = self.request.data['games']
        else:
            game_slugs = []
        if game_slugs:
            return Game.objects.filter(slug__in=game_slugs)
        return Game.objects.all()


class GameDetailView(generics.RetrieveAPIView):
    serializer_class = GameSerializer
    lookup_field = 'slug'
    queryset = Game.objects.all()


def game_for_installer(request, slug):
    """ Redirects to the game details page from a valid installer slug """
    try:
        installers = models.Installer.objects.fuzzy_get(slug)
    except Installer.DoesNotExist:
        raise Http404
    installer = installers[0]
    game_slug = installer.game.slug
    return redirect(reverse('game_detail', kwargs={'slug': game_slug}))


def game_detail(request, slug):
    game = get_object_or_404(models.Game, slug=slug)
    banner_options = {'crop': 'top', 'blur': '14x6'}
    banner_size = "940x352"
    user = request.user
    game.website_text = game.website.strip('htps:').strip('/')

    if user.is_authenticated():
        in_library = game in user.gamelibrary.games.all()
        installers = game.installer_set.published(user=user,
                                                  is_staff=user.is_staff)
        screenshots = game.screenshot_set.published(user=user,
                                                    is_staff=user.is_staff)
    else:
        in_library = False
        installers = game.installer_set.published()
        screenshots = game.screenshot_set.published()

    # auto_installers = game.get_default_installers()
    auto_installers = []
    return render(request, 'games/detail.html',
                  {'game': game,
                   'banner_options': banner_options,
                   'banner_size': banner_size,
                   'in_library': in_library,
                   'installers': installers,
                   'auto_installers': auto_installers,
                   'screenshots': screenshots})


@login_required
def new_installer(request, slug):
    game = get_object_or_404(models.Game, slug=slug)
    installer = Installer(game=game)
    installer.set_default_installer()
    form = InstallerForm(request.POST or None, instance=installer)
    if request.method == 'POST' and form.is_valid():
        installer = form.save(commit=False)
        installer.game_id = game.id
        installer.user = request.user
        installer.save()
        return redirect("installer_complete", slug=game.slug)
    return render(request, 'games/installer-form.html',
                  {'form': form, 'game': game})


@login_required
def publish_installer(request, slug):
    installer = get_object_or_404(Installer, slug=slug)
    if not request.user.is_staff:
        raise Http404
    installer.published = True
    installer.save()
    return redirect('game_detail', slug=installer.game.slug)


def validate(game, request, form):
    if request.method == 'POST' and form.is_valid():
        installer = form.save(commit=False)
        installer.game_id = game.id
        installer.user_id = request.user.id
        installer.save()


@login_required
def edit_installer(request, slug):
    installer = get_object_or_404(Installer, slug=slug)
    if installer.user != request.user and not request.user.is_staff:
        raise Http404
    form = InstallerForm(request.POST or None, instance=installer)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect("installer_complete", slug=installer.game.slug)
    return render(request, 'games/installer-form.html',
                  {'form': form, 'game': installer.game, 'new': False})


def installer_complete(request, slug):
    game = get_object_or_404(models.Game, slug=slug)
    return render(request, 'games/installer-complete.html', {'game': game})


def serve_installer(_request, slug):
    """Serve the content of an installer in yaml format."""
    try:
        installers = Installer.objects.fuzzy_get(slug)
    except Installer.DoesNotExist:
        raise Http404
    installer = installers[0]
    return HttpResponse(installer.as_yaml(), content_type='application/yaml')


def get_installers(request, slug):
    try:
        installers_json = Installer.objects.get_json(slug)
    except Installer.DoesNotExist:
        raise Http404
    return HttpResponse(installers_json, content_type='application/json')


class InstallerFeed(Feed):
    title = "Lutris installers"
    link = '/games/'
    description = u"Latest lutris installers"
    feed_size = 20

    def items(self):
        return Installer.objects.order_by("-created_at")[:self.feed_size]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.content

    def item_link(self, item):
        return item.get_absolute_url()


def get_game_by_slug(slug):
    """Return game matching an installer slug or game slug"""
    game = None
    try:
        installers = Installer.objects.fuzzy_get(slug)
        installer = installers[0]
        game = installer.game
    except Installer.DoesNotExist:
        try:
            game = models.Game.objects.get(slug=slug)
        except models.Game.DoesNotExist:
            pass
    return game


def get_banner(request, slug):
    """Serve game title in an appropriate format for the client."""
    game = get_game_by_slug(slug)
    if not game or not game.title_logo:
        raise Http404
    thumbnail = get_thumbnail(game.title_logo, settings.BANNER_SIZE,
                              crop="center")
    return redirect(thumbnail.url)


def serve_installer_banner(request, slug):
    return get_banner(request, slug)


def get_icon(request, slug):
    game = get_game_by_slug(slug)
    if not game or not game.icon:
        raise Http404
    thumbnail = get_thumbnail(game.icon, settings.ICON_SIZE, crop="center",
                              format="PNG")
    return redirect(thumbnail.url)


def serve_installer_icon(request, slug):
    """ Legacy url, remove by Lutris 0.4 """
    return get_icon(request, slug)


def game_list(request):
    """View for all games"""
    games = models.Game.objects.all()
    return render(request, 'games/game_list.html', {'games': games})


# def games_by_runner(request, runner_slug):
#     """View for games filtered by runner"""
#     runner = get_object_or_404(Runner, slug=runner_slug)
#     games = models.Game.objects.filter(runner__slug=runner.slug)
#     return render(request, 'games/game_list.html',
#                   {'games': games})


@login_required
def submit_game(request):
    form = GameForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        game = form.save()
        submission = GameSubmission(user=request.user, game=game)
        submission.save()

        # Notify managers a game has been submitted
        subject = u"New game submitted: {0}".format(game.name)
        admin_url = reverse("admin:games_game_change", args=(game.id, ))
        body = u"""
        The game {0} has been added by {1}.

        It can be modified and published at https://lutris.net{2}
        """.format(game.name, request.user, admin_url)
        mail_managers(subject, body)
        redirect_url = request.build_absolute_uri(reverse("game-submitted"))

        # Enforce https
        if not settings.DEBUG:
            redirect_url = redirect_url.replace('http:', 'https:')

        LOGGER.info('Game submitted, redirecting to %s', redirect_url)
        return redirect(redirect_url)
    return render(request, 'games/submit.html', {'form': form})


@login_required
def screenshot_add(request, slug):
    game = get_object_or_404(Game, slug=slug)
    form = ScreenshotForm(request.POST or None, request.FILES or None,
                          game_id=game.id)
    if form.is_valid():
        form.instance.uploaded_by = request.user
        form.save()
        return redirect(reverse("game_detail", kwargs={'slug': slug}))
    return render(request, 'games/screenshot/add.html', {'form': form})


@login_required
def publish_screenshot(request, id):
    screenshot = get_object_or_404(models.Screenshot, id=id)
    if not request.user.is_staff:
        raise Http404
    screenshot.published = True
    screenshot.save()
    return redirect('game_detail', slug=screenshot.game.slug)


@require_POST
@login_required
def submit_issue(request):
    response = {'status': 'ok'}
    try:
        installer = Installer.objects.get(slug=request.POST.get('installer'))
    except Installer.DoesNotExist:
        response['status'] = 'error'
        response['message'] = 'Could not find the installer'
        return HttpResponse(json.dumps(response))

    content = request.POST.get('content')
    if not content:
        response['status'] = 'error'
        response['message'] = 'The issue content is empty'
        return HttpResponse(json.dumps(response))

    user = request.user
    installer_issue = InstallerIssue(
        installer=installer,
        submitted_by=user,
        description=content
    )
    installer_issue.save()

    return HttpResponse(json.dumps(response))
