import hangups, re, urllib, json

from hangupsbot.handlers import handler


@handler.register(priority=7, event=hangups.ChatMessageEvent)
def handle_forward(bot, event):
    """Handle message forwarding"""
    # Test if message is not empty
    if not event.text:
        return

    # Test if message forwarding is enabled
    if not bot.get_config_suboption(event.conv_id, 'forwarding_enabled'):
        return

    forward_to_list = bot.get_config_suboption(event.conv_id, 'forward_to')
    if forward_to_list:
        for dst in forward_to_list:
            if re.match('^(http|https)://(.*)$',dst):
                payload = {}
                payload['chat_id'] = event.conv_id;
                payload['from'] = {
                    'id': event.user_id.chat_id,
                    'full_name' : event.user.full_name,
                    'emails' : event.user.emails,
                    'first_name': event.user.first_name,
                    'photo_url': event.user.photo_url
                }
                payload['timestamp'] = str(event.timestamp)
                payload['message'] = {
                    'text': event.text
                }
                #pprint(getmembers(event))    
                #pprint(getmembers(event.user))
                print('Relaying message to: ' + dst + ' from: ' + event.conv_id + ' @ ' + event.user.full_name + ' - message: ' + event.text)
                request = urllib.request.Request(dst, data=json.dumps(payload).encode('utf8'), method='POST', headers={'Content-Type': 'application/json'})
                try:
                    response = urllib.request.urlopen(request)
                except (urllib.error.HTTPError, urllib.error.URLError) as error:
                    print('Could not connect to message gateway: ' + str(error.reason))                
            else:
                try:
                    conv = bot._conv_list.get(dst)
                except KeyError:
                    continue

                # Prepend forwarded message with name of sender
                link = 'https://plus.google.com/u/0/{}/about'.format(event.user_id.chat_id)
                segments = [hangups.ChatMessageSegment(event.user.full_name, hangups.SegmentType.LINK,
                                                       link_target=link, is_bold=True),
                            hangups.ChatMessageSegment(': ', is_bold=True)]
                # Copy original message segments
                segments.extend(event.conv_event.segments)
                # Append links to attachments (G+ photos) to forwarded message
                if event.conv_event.attachments:
                    segments.append(hangups.ChatMessageSegment('\n', hangups.SegmentType.LINE_BREAK))
                    segments.extend([hangups.ChatMessageSegment(link, hangups.SegmentType.LINK, link_target=link)
                                     for link in event.conv_event.attachments])
                bot.send_message_segments(conv, segments)
