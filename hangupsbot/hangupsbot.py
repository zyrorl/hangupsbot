#!/usr/bin/env python3

# Install modern gettext class-based API in Python's builtins namespace first
import os, gettext
localedir = os.path.join(os.path.dirname(__file__), 'locale')
gettext.install('hangupsbot', localedir=localedir)

# For argparse localization to work, we need also to setup old GNU gettext API
gettext.bindtextdomain('hangupsbot', localedir=localedir)
gettext.textdomain('hangupsbot')

import sys, argparse, logging, shutil, asyncio, time, signal, urllib, json

import appdirs
import hangups
from hangups.conversation import Conversation
from hangups.ui.utils import get_conv_name

import hangupsbot.config
from hangupsbot.version import __version__
from hangupsbot.handlers import handler
from pprint import pprint


LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


class HangupsBot:
    """Hangouts bot listening on all conversations"""
    def __init__(self, cookies_path, config_path, max_retries=5):
        self._client = None
        self._cookies_path = cookies_path
        self._max_retries = max_retries

        # These are populated by on_connect when it's called.
        self._conv_list = None        # hangups.ConversationList
        self._user_list = None        # hangups.UserList

        # Load config file
        self.config = hangupsbot.config.Config(config_path)

        # Handle signals on Unix
        # (add_signal_handler is not implemented on Windows)
        try:
            loop = asyncio.get_event_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, lambda: self.stop())
        except NotImplementedError:
            pass

    def login(self, cookies_path):
        """Login to Google account"""
        # Authenticate Google user and save auth cookies
        # (or load already saved cookies)
        try:
            cookies = hangups.auth.get_auth_stdin(cookies_path)
            return cookies
        except hangups.GoogleAuthError as e:
            print(_('Login failed ({})').format(e))
            return False

    def run(self):
        """Connect to Hangouts and run bot"""
        cookies = self.login(self._cookies_path)
        if cookies:
            for retry in range(self._max_retries):
                try:
                    # Create Hangups client
                    self._client = hangups.Client(cookies)
                    self._client.on_connect.add_observer(self._on_connect)
                    self._client.on_disconnect.add_observer(self._on_disconnect)

                    # Start asyncio event loop and connect to Hangouts
                    # If we are forcefully disconnected, try connecting again
                    loop = asyncio.get_event_loop()
                    loop.run_until_complete(self._client.connect())
                    sys.exit(0)
                except Exception as e:
                    print(_('Client unexpectedly disconnected:\n{}').format(e))
                    print(_('Waiting {} seconds...').format(5 + retry * 5))
                    time.sleep(5 + retry * 5)
                    print(_('Trying to connect again (try {} of {})...').format(retry + 1, self._max_retries))
            print(_('Maximum number of retries reached! Exiting...'))
        sys.exit(1)

    @asyncio.coroutine
    def get_queue_messages(self):
        while True:
            request = urllib.request.Request('http://localhost:3000/queue/hangouts-bot')
            try:
                response = urllib.request.urlopen(request).read().decode('utf8')
            except (urllib.error.HTTPError, urllib.error.URLError) as error:
                print('Could not connect to message gateway: ' + str(error.reason))
                yield from asyncio.sleep(1)
            else:
                slackMessages = json.loads(response)
                if slackMessages:
                  for messageObj in slackMessages:
                    route = messageObj['route']
                    message = messageObj['message']
                    conv = self._conv_list.get(route['to'])
                    if not message['from']['username'] == "USLACKBOT":
                        print('Relaying message to: ' + route['to'] + ' - from: ' + message['from']['username'] + ' - message: ' + message['message']['text'])
                        segments = [
                                    hangups.ChatMessageSegment('@' + message['from']['username'] + ': ', is_bold=True),
                                    hangups.ChatMessageSegment(message['message']['text'])
                                   ]
                        self.send_message_segments(conv, segments)
            yield from asyncio.sleep(1)

    def stop(self):
        """Disconnect from Hangouts"""
        asyncio.async(
            self._client.disconnect()
        ).add_done_callback(lambda future: future.result())

    def send_message(self, conversation, text):
        """Send simple chat message"""
        self.send_message_segments(conversation, [hangups.ChatMessageSegment(text)])

    def send_message_segments(self, conversation, segments):
        """Send chat message segments"""
        # Ignore if the user hasn't typed a message.
        if len(segments) == 0:
            return
        # XXX: Exception handling here is still a bit broken. Uncaught
        # exceptions in _on_message_sent will only be logged.
        asyncio.async(
            conversation.send_message(segments)
        ).add_done_callback(self._on_message_sent)

    def list_conversations(self):
        """List all active conversations"""
        convs = sorted(self._conv_list.get_all(),
                       reverse=True, key=lambda c: c.last_modified)
        return convs

    def find_conversations(self, conv_name):
        """Find conversations by name or ID in list of all active conversations"""
        conv_name = conv_name.strip()
        conv_name_lower = conv_name.lower()
        if conv_name_lower.startswith("id:"):
            return [self._conv_list.get(conv_name[3:])]

        convs = [c for c in self.list_conversations()
                 if conv_name_lower in get_conv_name(c, truncate=True).lower()]
        return convs

    def list_users(self, conv=None):
        """List all known users or all users in conversation"""
        def full_name_sort(user):
            split_name = user.full_name.split()
            return (split_name[-1], split_name[0])
        users = conv.users if isinstance(conv, Conversation) else self._user_list.get_all()
        return sorted(users, key=full_name_sort)

    def find_users(self, user_name, conv=None):
        """Find users by name or ID in list of all known users or in conversation"""
        user_name = user_name.strip()
        user_name_lower = user_name.lower()
        if user_name_lower.startswith("id:"):
            return [self._user_list.get(user_name[3:])]

        users = [u for u in self.list_users(conv=conv)
                 if user_name_lower in u.full_name.lower()]
        return users

    def get_config_suboption(self, conv_id, option):
        """Get config suboption for conversation (or global option if not defined)"""
        try:
            suboption = self.config['conversations'][conv_id][option]
        except KeyError:
            try:
                suboption = self.config[option]
            except KeyError:
                suboption = None
        return suboption

    def _on_message_sent(self, future):
        """Handle showing an error if a message fails to send"""
        try:
            future.result()
        except hangups.NetworkError:
            print(_('Failed to send message!'))

    def _on_connect(self, initial_data):
        """Handle connecting for the first time"""
        print(_('Connected!'))
        self._user_list = hangups.UserList(self._client,
                                           initial_data.self_entity,
                                           initial_data.entities,
                                           initial_data.conversation_participants)
        self._conv_list = hangups.ConversationList(self._client,
                                                   initial_data.conversation_states,
                                                   self._user_list,
                                                   initial_data.sync_timestamp)
        self._conv_list.on_event.add_observer(self._on_event)

        print(_('Conversations:'))
        for c in self.list_conversations():
            print('  {} ({})'.format(get_conv_name(c, truncate=True), c.id_))
        print()
        asyncio.async(self.get_queue_messages())

    def _on_event(self, conv_event):
        """Handle conversation events"""
        asyncio.async(handler.handle(self, conv_event))

    def _on_disconnect(self):
        """Handle disconnecting"""
        print(_('Connection lost!'))


def main():
    """Main entry point"""
    # Build default paths for files.
    dirs = appdirs.AppDirs('hangupsbot', 'hangupsbot')
    default_log_path = os.path.join(dirs.user_data_dir, 'hangupsbot.log')
    default_cookies_path = os.path.join(dirs.user_data_dir, 'cookies.json')
    default_config_path = os.path.join(dirs.user_data_dir, 'config.json')

    # Configure argument parser
    parser = argparse.ArgumentParser(prog='hangupsbot',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-d', '--debug', action='store_true',
                        help=_('log detailed debugging messages'))
    parser.add_argument('--log', default=default_log_path,
                        help=_('log file path'))
    parser.add_argument('--cookies', default=default_cookies_path,
                        help=_('cookie storage path'))
    parser.add_argument('--config', default=default_config_path,
                        help=_('config storage path'))
    parser.add_argument('--version', action='version', version='%(prog)s {}'.format(__version__),
                        help=_('show program\'s version number and exit'))
    args = parser.parse_args()

    # Create all necessary directories.
    for path in [args.log, args.cookies, args.config]:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory)
            except OSError as e:
                sys.exit(_('Failed to create directory: {}').format(e))

    # If there is no config file in user data directory, copy default one there
    if not os.path.isfile(args.config):
        try:
            shutil.copy(os.path.abspath(os.path.join(os.path.dirname(__file__), 'config.json')),
                        args.config)
        except (OSError, IOError) as e:
            sys.exit(_('Failed to copy default config file: {}').format(e))

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(filename=args.log, level=log_level, format=LOG_FORMAT)
    # asyncio's debugging logs are VERY noisy, so adjust the log level
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    # Start Hangups bot
    bot = HangupsBot(args.cookies, args.config)
    bot.run()


if __name__ == '__main__':
    main()
