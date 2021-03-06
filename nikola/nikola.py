# -*- coding: utf-8 -*-

import codecs
from collections import defaultdict
from copy import copy
import datetime
import glob
import os
import sys
import urlparse

from doit.tools import PythonInteractiveAction
import lxml.html
from yapsy.PluginManager import PluginManager

if os.getenv('DEBUG'):
    import logging
    logging.basicConfig(level=logging.DEBUG)
else:
    import logging
    logging.basicConfig(level=logging.ERROR)

from post import Post
import utils
from plugin_categories import (
    Command,
    LateTask,
    PageCompiler,
    Task,
    TemplateSystem,
)

config_changed = utils.config_changed

__all__ = ['Nikola']


class Nikola(object):

    """Class that handles site generation.

    Takes a site config as argument on creation.
    """

    def __init__(self, **config):
        """Setup proper environment for running tasks."""

        self.global_data = {}
        self.posts_per_year = defaultdict(list)
        self.posts_per_tag = defaultdict(list)
        self.timeline = []
        self.pages = []
        self._scanned = False

        # This is the default config
        # TODO: fill it
        self.config = {
            'ARCHIVE_PATH': "",
            'ARCHIVE_FILENAME': "archive.html",
            'DEFAULT_LANG': "en",
            'OUTPUT_FOLDER': 'output',
            'FILES_FOLDERS': {'files': ''},
            'LISTINGS_FOLDER': 'listings',
            'ADD_THIS_BUTTONS': True,
            'INDEX_DISPLAY_POST_COUNT': 10,
            'INDEX_TEASERS': False,
            'MAX_IMAGE_SIZE': 1280,
            'USE_FILENAME_AS_TITLE': True,
            'SLUG_TAG_PATH': False,
            'INDEXES_TITLE': "",
            'INDEXES_PAGES': "",
            'FILTERS': {},
            'USE_BUNDLES': True,
            'TAG_PAGES_ARE_INDEXES': False,
            'THEME': 'default',
            'post_compilers': {
                "rest":     ['.txt', '.rst'],
                "markdown": ['.md', '.mdown', '.markdown'],
                "html": ['.html', '.htm'],
            },
        }
        self.config.update(config)
        self.config['TRANSLATIONS'] = self.config.get('TRANSLATIONS',
            {self.config['DEFAULT_LANG']: ''})

        # FIXME: find  way to achieve this with the plugins
        #if self.config['USE_BUNDLES'] and not webassets:
            #self.config['USE_BUNDLES'] = False

        self.GLOBAL_CONTEXT = self.config.get('GLOBAL_CONTEXT', {})
        self.THEMES = utils.get_theme_chain(self.config['THEME'])

        self.MESSAGES = utils.load_messages(self.THEMES,
            self.config['TRANSLATIONS'])
        self.GLOBAL_CONTEXT['messages'] = self.MESSAGES

        self.GLOBAL_CONTEXT['_link'] = self.link
        self.GLOBAL_CONTEXT['rel_link'] = self.rel_link
        self.GLOBAL_CONTEXT['abs_link'] = self.abs_link
        self.GLOBAL_CONTEXT['exists'] = self.file_exists
        self.GLOBAL_CONTEXT['add_this_buttons'] = self.config[
            'ADD_THIS_BUTTONS']
        self.GLOBAL_CONTEXT['index_display_post_count'] = self.config[
            'INDEX_DISPLAY_POST_COUNT']
        self.GLOBAL_CONTEXT['use_bundles'] = self.config['USE_BUNDLES']

        self.plugin_manager = PluginManager(categories_filter={
            "Command": Command,
            "Task": Task,
            "LateTask": LateTask,
            "TemplateSystem": TemplateSystem,
            "PageCompiler": PageCompiler,
        })
        self.plugin_manager.setPluginInfoExtension('plugin')
        self.plugin_manager.setPluginPlaces([
            os.path.join(os.path.dirname(__file__), 'plugins'),
            os.path.join(os.getcwd(), 'plugins'),
            ])
        self.plugin_manager.collectPlugins()

        self.commands = {}
        # Activate all command plugins
        for pluginInfo in self.plugin_manager.getPluginsOfCategory("Command"):
            self.plugin_manager.activatePluginByName(pluginInfo.name)
            pluginInfo.plugin_object.set_site(self)
            pluginInfo.plugin_object.short_help = pluginInfo.description
            self.commands[pluginInfo.name] = pluginInfo.plugin_object

        # Activate all task plugins
        for pluginInfo in self.plugin_manager.getPluginsOfCategory("Task"):
            self.plugin_manager.activatePluginByName(pluginInfo.name)
            pluginInfo.plugin_object.set_site(self)

        for pluginInfo in self.plugin_manager.getPluginsOfCategory("LateTask"):
            self.plugin_manager.activatePluginByName(pluginInfo.name)
            pluginInfo.plugin_object.set_site(self)

        # Load template plugin
        template_sys_name = utils.get_template_engine(self.THEMES)
        pi = self.plugin_manager.getPluginByName(
            template_sys_name, "TemplateSystem")
        if pi is None:
            sys.stderr.write("Error loading %s template system plugin\n"
                % template_sys_name)
            sys.exit(1)
        self.template_system = pi.plugin_object
        self.template_system.set_directories(
            [os.path.join(utils.get_theme_path(name), "templates")
                for name in self.THEMES])

        # Load compiler plugins
        self.compilers = {}
        self.inverse_compilers = {}

        for pluginInfo in self.plugin_manager.getPluginsOfCategory(
                "PageCompiler"):
            self.compilers[pluginInfo.name] = \
                pluginInfo.plugin_object.compile_html

    def get_compiler(self, source_name):
        """Get the correct compiler for a post from `conf.post_compilers`

        To make things easier for users, the mapping in conf.py is
        compiler->[extensions], although this is less convenient for us. The
        majority of this function is reversing that dictionary and error
        checking.
        """
        ext = os.path.splitext(source_name)[1]
        try:
            compile_html = self.inverse_compilers[ext]
        except KeyError:
            # Find the correct compiler for this files extension
            langs = [lang for lang, exts in
                     self.config['post_compilers'].items()
                     if ext in exts]
            if len(langs) != 1:
                if len(set(langs)) > 1:
                    exit("Your file extension->compiler definition is"
                         "ambiguous.\nPlease remove one of the file extensions"
                         "from 'post_compilers' in conf.py\n(The error is in"
                         "one of %s)" % ', '.join(langs))
                elif len(langs) > 1:
                    langs = langs[:1]
                else:
                    exit("post_compilers in conf.py does not tell me how to "
                         "handle '%s' extensions." % ext)

            lang = langs[0]
            compile_html = self.compilers[lang]
            self.inverse_compilers[ext] = compile_html

        return compile_html

    def render_template(self, template_name, output_name, context):
        data = self.template_system.render_template(
            template_name, None, context, self.GLOBAL_CONTEXT)

        assert output_name.startswith(self.config["OUTPUT_FOLDER"])
        url_part = output_name[len(self.config["OUTPUT_FOLDER"]) + 1:]

        # This is to support windows paths
        url_part = "/".join(url_part.split(os.sep))

        src = urlparse.urljoin(self.config["BLOG_URL"], url_part)

        parsed_src = urlparse.urlsplit(src)
        src_elems = parsed_src.path.split('/')[1:]

        def replacer(dst):
            # Refuse to replace links that are full URLs.
            dst_url = urlparse.urlparse(dst)
            if dst_url.netloc:
                if dst_url.scheme == 'link':  # Magic link
                    dst = self.link(dst_url.netloc, dst_url.path.lstrip('/'),
                        context['lang'])
                else:
                    return dst

            # Normalize
            dst = urlparse.urljoin(src, dst)
            # Avoid empty links.
            if src == dst:
                return "#"
            # Check that link can be made relative, otherwise return dest
            parsed_dst = urlparse.urlsplit(dst)
            if parsed_src[:2] != parsed_dst[:2]:
                return dst

            # Now both paths are on the same site and absolute
            dst_elems = parsed_dst.path.split('/')[1:]

            i = 0
            for (i, s), d in zip(enumerate(src_elems), dst_elems):
                if s != d:
                    break
            # Now i is the longest common prefix
            result = '/'.join(['..'] * (len(src_elems) - i - 1) +
                dst_elems[i:])

            if not result:
                result = "."

            # Don't forget the fragment (anchor) part of the link
            if parsed_dst.fragment:
                result += "#" + parsed_dst.fragment

            assert result, (src, dst, i, src_elems, dst_elems)

            return result

        try:
            os.makedirs(os.path.dirname(output_name))
        except:
            pass
        doc = lxml.html.document_fromstring(data)
        doc.rewrite_links(replacer)
        data = '<!DOCTYPE html>' + lxml.html.tostring(doc, encoding='utf8')
        with open(output_name, "w+") as post_file:
            post_file.write(data)

    def path(self, kind, name, lang, is_link=False):
        """Build the path to a certain kind of page.

        kind is one of:

        * tag_index (name is ignored)
        * tag (and name is the tag name)
        * tag_rss (name is the tag name)
        * archive (and name is the year, or None for the main archive index)
        * index (name is the number in index-number)
        * rss (name is ignored)
        * gallery (name is the gallery name)
        * listing (name is the source code file name)

        The returned value is always a path relative to output, like
        "categories/whatever.html"

        If is_link is True, the path is absolute and uses "/" as separator
        (ex: "/archive/index.html").
        If is_link is False, the path is relative to output and uses the
        platform's separator.
        (ex: "archive\\index.html")
        """

        path = []

        if kind == "tag_index":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], 'index.html'])
        elif kind == "tag":
            if self.config['SLUG_TAG_PATH']:
                name = utils.slugify(name)
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], name + ".html"])
        elif kind == "tag_rss":
            if self.config['SLUG_TAG_PATH']:
                name = utils.slugify(name)
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], name + ".xml"])
        elif kind == "index":
            if name > 0:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['INDEX_PATH'], 'index-%s.html' % name])
            else:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['INDEX_PATH'], 'index.html'])
        elif kind == "rss":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['RSS_PATH'], 'rss.xml'])
        elif kind == "archive":
            if name:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['ARCHIVE_PATH'], name, 'index.html'])
            else:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['ARCHIVE_PATH'], self.config['ARCHIVE_FILENAME']])
        elif kind == "gallery":
            path = filter(None,
                [self.config['GALLERY_PATH'], name, 'index.html'])
        elif kind == "listing":
            path = filter(None,
                [self.config['LISTINGS_FOLDER'], name + '.html'])
        if is_link:
            return '/' + ('/'.join(path))
        else:
            return os.path.join(*path)

    def link(self, *args):
        return self.path(*args, is_link=True)

    def abs_link(self, dst):
        # Normalize
        dst = urlparse.urljoin(self.config['BLOG_URL'], dst)

        return urlparse.urlparse(dst).path

    def rel_link(self, src, dst):
        # Normalize
        src = urlparse.urljoin(self.config['BLOG_URL'], src)
        dst = urlparse.urljoin(src, dst)
        # Avoid empty links.
        if src == dst:
            return "#"
        # Check that link can be made relative, otherwise return dest
        parsed_src = urlparse.urlsplit(src)
        parsed_dst = urlparse.urlsplit(dst)
        if parsed_src[:2] != parsed_dst[:2]:
            return dst
        # Now both paths are on the same site and absolute
        src_elems = parsed_src.path.split('/')[1:]
        dst_elems = parsed_dst.path.split('/')[1:]
        i = 0
        for (i, s), d in zip(enumerate(src_elems), dst_elems):
            if s != d:
                break
        else:
            i += 1
        # Now i is the longest common prefix
        return '/'.join(['..'] * (len(src_elems) - i - 1) + dst_elems[i:])

    def file_exists(self, path, not_empty=False):
        """Returns True if the file exists. If not_empty is True,
        it also has to be not empty."""
        exists = os.path.exists(path)
        if exists and not_empty:
            exists = os.stat(path).st_size > 0
        return exists

    def gen_tasks(self):
        yield self.gen_task_new_post(self.config['post_pages'])
        yield self.gen_task_new_page(self.config['post_pages'])

        task_dep = []
        for pluginInfo in self.plugin_manager.getPluginsOfCategory("Task"):
            for task in pluginInfo.plugin_object.gen_tasks():
                yield task
            if pluginInfo.plugin_object.is_default:
                task_dep.append(pluginInfo.plugin_object.name)

        for pluginInfo in self.plugin_manager.getPluginsOfCategory("LateTask"):
            for task in pluginInfo.plugin_object.gen_tasks():
                yield task
            if pluginInfo.plugin_object.is_default:
                task_dep.append(pluginInfo.plugin_object.name)

        yield {
            'name': 'all',
            'actions': None,
            'clean': True,
            'task_dep': task_dep
        }

    def scan_posts(self):
        """Scan all the posts."""
        if not self._scanned:
            print "Scanning posts ",
            targets = set([])
            for wildcard, destination, _, use_in_feeds in \
                    self.config['post_pages']:
                print ".",
                for base_path in glob.glob(wildcard):
                    post = Post(base_path, destination, use_in_feeds,
                        self.config['TRANSLATIONS'],
                        self.config['DEFAULT_LANG'],
                        self.config['BLOG_URL'],
                        self.MESSAGES)
                    for lang, langpath in self.config['TRANSLATIONS'].items():
                        dest = (destination, langpath, post.pagenames[lang])
                        if dest in targets:
                            raise Exception(
                                'Duplicated output path %r in post %r' %
                                (post.pagenames[lang], base_path))
                        targets.add(dest)
                    self.global_data[post.post_name] = post
                    if post.use_in_feeds:
                        self.posts_per_year[
                            str(post.date.year)].append(post.post_name)
                        for tag in post.tags:
                            self.posts_per_tag[tag].append(post.post_name)
                    else:
                        self.pages.append(post)
            for name, post in self.global_data.items():
                self.timeline.append(post)
            self.timeline.sort(cmp=lambda a, b: cmp(a.date, b.date))
            self.timeline.reverse()
            post_timeline = [p for p in self.timeline if p.use_in_feeds]
            for i, p in enumerate(post_timeline[1:]):
                p.next_post = post_timeline[i]
            for i, p in enumerate(post_timeline[:-1]):
                p.prev_post = post_timeline[i + 1]
            self._scanned = True
            print "done!"

    def generic_page_renderer(self, lang, wildcard,
        template_name, destination, filters):
        """Render post fragments to final HTML pages."""
        for post in glob.glob(wildcard):
            post_name = os.path.splitext(post)[0]
            context = {}
            post = self.global_data[post_name]
            deps = post.deps(lang) + \
                self.template_system.template_deps(template_name)
            context['post'] = post
            context['lang'] = lang
            context['title'] = post.title(lang)
            context['description'] = post.description(lang)
            context['permalink'] = post.permalink(lang)
            context['page_list'] = self.pages
            output_name = os.path.join(
                self.config['OUTPUT_FOLDER'],
                self.config['TRANSLATIONS'][lang],
                destination,
                post.pagenames[lang] + ".html")
            deps_dict = copy(context)
            deps_dict.pop('post')
            if post.prev_post:
                deps_dict['PREV_LINK'] = [post.prev_post.permalink(lang)]
            if post.next_post:
                deps_dict['NEXT_LINK'] = [post.next_post.permalink(lang)]
            deps_dict['OUTPUT_FOLDER'] = self.config['OUTPUT_FOLDER']
            deps_dict['TRANSLATIONS'] = self.config['TRANSLATIONS']

            task = {
                'name': output_name.encode('utf-8'),
                'file_dep': deps,
                'targets': [output_name],
                'actions': [(self.render_template,
                    [template_name, output_name, context])],
                'clean': True,
                'uptodate': [config_changed(deps_dict)],
            }

            yield utils.apply_filters(task, filters)

    def generic_post_list_renderer(self, lang, posts,
        output_name, template_name, filters, extra_context):
        """Renders pages with lists of posts."""

        deps = self.template_system.template_deps(template_name)
        for post in posts:
            deps += post.deps(lang)
        context = {}
        context["posts"] = posts
        context["title"] = self.config['BLOG_TITLE']
        context["description"] = self.config['BLOG_DESCRIPTION']
        context["lang"] = lang
        context["prevlink"] = None
        context["nextlink"] = None
        context.update(extra_context)
        deps_context = copy(context)
        deps_context["posts"] = [(p.titles[lang], p.permalink(lang))
            for p in posts]
        task = {
            'name': output_name.encode('utf8'),
            'targets': [output_name],
            'file_dep': deps,
            'actions': [(self.render_template,
                [template_name, output_name, context])],
            'clean': True,
            'uptodate': [config_changed(deps_context)]
        }

        yield utils.apply_filters(task, filters)

    @staticmethod
    def new_post(post_pages, is_post=True):
        # Guess where we should put this
        for path, _, _, use_in_rss in post_pages:
            if use_in_rss == is_post:
                break
        else:
            path = post_pages[0][0]

        print "Creating New Post"
        print "-----------------\n"
        title = raw_input("Enter title: ").decode(sys.stdin.encoding)
        slug = utils.slugify(title)
        data = u'\n'.join([
            title,
            slug,
            datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
            ])
        output_path = os.path.dirname(path)
        meta_path = os.path.join(output_path, slug + ".meta")
        pattern = os.path.basename(path)
        if pattern.startswith("*."):
            suffix = pattern[1:]
        else:
            suffix = ".txt"
        txt_path = os.path.join(output_path, slug + suffix)

        if os.path.isfile(meta_path) or os.path.isfile(txt_path):
            print "The title already exists!"
            exit()

        with codecs.open(meta_path, "wb+", "utf8") as fd:
            fd.write(data)
        with codecs.open(txt_path, "wb+", "utf8") as fd:
            fd.write(u"Write your post here.")
        print "Your post's metadata is at: ", meta_path
        print "Your post's text is at: ", txt_path

    @classmethod
    def new_page(cls):
        cls.new_post(False)

    @classmethod
    def gen_task_new_post(cls, post_pages):
        """Create a new post (interactive)."""
        yield {
            "basename": "new_post",
            "actions": [PythonInteractiveAction(cls.new_post, (post_pages,))],
            }

    @classmethod
    def gen_task_new_page(cls, post_pages):
        """Create a new post (interactive)."""
        yield {
            "basename": "new_page",
            "actions": [PythonInteractiveAction(cls.new_post,
                (post_pages, False,))],
            }
