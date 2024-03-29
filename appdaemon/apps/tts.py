"""
Plays a TTS message on Amazon Echo devices using Alexa notification service.
A list of target devices is generated based on time, recent motion activity,
and a set of default and last resort targets.

"""

__author__ = 'Arkadii Yakovets (ark@cho.red)'

# pylint: disable=attribute-defined-outside-init
# pylint: disable=import-error
# pylint: disable=too-many-instance-attributes

import hashlib
import re
import sys
import time
from collections import defaultdict
from queue import Queue
from threading import Thread

from appdaemon.plugins.hass import hassapi as hass


class AmazonEcho(hass.Hass):
  """Amazon Echo TTS App Daemon class."""

  EVENT_NAME = 'tts'
  STATE_OFF = 'off'
  STATE_ON = 'on'
  STATE_PLAYING = 'playing'
  THROTTLED_ENTITY_TIME_DEFAULT_SECONDS = 60
  TTS_CHARACTERS_PER_SECOND = 7

  def initialize(self):
    """Initialize event listener."""

    self.env = self.args['env']
    self.play_always_normal_time = []
    self.play_always_quite_time = []
    self.play_default_normal_time = []
    self.play_default_quite_time = []
    self.rules = self.args['rules']
    self.quite_time = self.args['quite_time']
    self.throttle = self.args['throttle']
    self.throttled_entity_time_mapping = defaultdict(
        lambda: self.THROTTLED_ENTITY_TIME_DEFAULT_SECONDS)
    self.throttled_events = {}

    self.configure_throttling()

    self.messages = Queue(maxsize=5)
    thread = Thread(target=self.worker)
    thread.daemon = True
    thread.start()

    self.listen_event(self.handle_event, self.EVENT_NAME)

  @staticmethod
  def calculate_duration(text):
    clean_text = re.sub(r'<.*?>', '', text)
    duration = round(len(clean_text) / AmazonEcho.TTS_CHARACTERS_PER_SECOND)

    if 'beeps_and_bloops/tone_05' in text:
      duration += 1

    return duration

  @staticmethod
  def get_target(area):
    """Return media player target ID for an area."""
    return f'media_player.{area}_echo'

  def configure_throttling(self):
    """Populates throttle rules from config values."""
    self.throttled_entity_time_mapping.update({
        entity_id: throttle_time for throttle_mapping in self.throttle
        for entity_id, throttle_time in throttle_mapping.items()
    })

  # pylint: disable=unused-argument
  def handle_event(self, event, data, kwargs):
    """Put new message to the queue."""
    entity_id = data.get('entity_id')
    text = data.get('text')

    if not self.is_throttled(entity_id, text):
      self.messages.put({
          'areas_off': data.get('areas_off'),
          'areas_on': data.get('areas_on'),
          'text': text,
      })

  def set_environment(self):
    """Add play always and play default area targets."""

    try:
      self.play_always_normal_time = [
          self.get_target(area)
          for area in self.env['play_always']['normal_time']
      ]
    except KeyError:
      pass

    try:
      self.play_always_quite_time = [
          self.get_target(area)
          for area in self.env['play_always']['quite_time']
      ]
    except KeyError:
      pass

    try:
      self.play_default_normal_time = [
          self.get_target(area)
          for area in self.env['play_default']['normal_time']
      ]
    except KeyError:
      pass

    try:
      self.play_default_quite_time = [
          self.get_target(area)
          for area in self.env['play_default']['quite_time']
      ]
    except KeyError:
      pass

  def is_throttled(self, entity_id, text):
    """Determines whether the `entity_id` event has to be throttled."""
    if not entity_id or not text:
      return False

    event_key = hashlib.blake2b(f'{entity_id}_{text}'.encode()).hexdigest()
    event_time = self.throttled_events.get(event_key)
    now = time.time()
    is_throttled = (
        event_time
        and now - event_time < self.throttled_entity_time_mapping[entity_id])

    if not is_throttled:
      self.throttled_events[event_key] = now

    return is_throttled

  # pylint: disable=too-many-branches
  def tts(self, text, areas_off=None, areas_on=None):
    """
      Check targets and generate text to speech API request.

      Parameters:
        text: A text to play.
        areas_off: A list of explicitly excluded areas.
        areas_on: A list of explicitly included areas.

      Returns:
        list: A list of target devices the message to be played on.
      """

    def in_dnd_mode(media_player):
      """
      Return True if media player device is in the "Do Not Disturb" mode.
      Return False otherwise.
      """
      return is_on(f'switch.{media_player.split(".")[1]}_do_not_disturb')

    def is_on(sensor):
      """Return True if sensor's state is 'on' otherwise returns False."""
      return self.get_state(sensor) == self.STATE_ON

    def is_playing(sensor):
      """Return True if sensor's state is 'playing' otherwise returns False."""
      return self.get_state(sensor) == self.STATE_PLAYING

    # tts()
    if not text:
      raise ValueError("Text field is required.")

    if areas_off == '*' and areas_on == '*':
      raise ValueError(
          "You can't use wildcard targets for both areas_off and areas_on at "
          "the same time.")

    targets_all = {self.rules[rule]['target'] for rule in self.rules}

    self.set_environment()

    for targets in (self.play_always_normal_time, self.play_always_quite_time,
                    self.play_default_normal_time,
                    self.play_default_quite_time):
      if targets:
        targets_all.update(targets)

    if areas_off == '*':
      targets_off = targets_all.copy()
    else:
      targets_off = {self.get_target(area) for area in areas_off or ()}

    if areas_on == '*':
      targets_on = targets_all.copy()
    else:
      targets_on = {self.get_target(area) for area in areas_on or ()}

    targets = set()

    # Normal time targets.
    if not is_on(self.quite_time):
      # Add targets based on the rule conditions.
      for area in self.rules:
        rule = self.rules[area]

        if any((is_on(c) for c in rule['conditions'])):
          targets.add(rule['target'])

      # Remove targets based on the if_not conditions.
      for area in self.rules:
        rule = self.rules[area]
        rule_target = rule['target']

        if rule_target not in targets or 'if_not' not in rule:
          continue

        conditions = rule['if_not'].get('conditions', ())
        target = rule['if_not'].get('target')

        if target and target not in targets:
          continue

        if not conditions or any((is_on(c) for c in conditions)):
          targets.remove(rule_target)

    # Add currently playing media players.
    for target in targets_all:
      if is_playing(target):
        targets.add(target)

    # Update targets based on areas_off/areas_on values.
    targets = targets.difference(targets_off)

    # Override areas_off with areas_on.
    targets.update(targets_on)

    targets_play_always = None
    targets_play_default = None
    if is_on(self.quite_time):
      targets_play_always = self.play_always_quite_time
      targets_play_default = self.play_default_quite_time
    else:
      targets_play_always = self.play_always_normal_time
      targets_play_default = self.play_default_normal_time

    if targets_play_always:
      targets.update(set(targets_play_always).difference(targets_off))

    if not targets and targets_play_default:
      targets.update(set(targets_play_default).difference(targets_off))

    targets = sorted((target for target in targets if not in_dnd_mode(target)))
    if targets:
      self.call_service('notify/alexa_media',
                        data={'type': 'tts'},
                        message=text,
                        target=targets)
    return targets

  def worker(self):
    """Process TTS messages from the queue."""

    while True:
      try:
        data = self.messages.get()
        areas_off = data['areas_off']
        areas_on = data['areas_on']
        text = data['text']

        targets = self.tts(areas_off=areas_off, areas_on=areas_on, text=text)
        duration = self.calculate_duration(text)

        self.log(f"{text} on {', '.join(targets)} ({duration}s)")
        time.sleep(duration)
      except Exception:  # pylint: disable=broad-except
        self.log(sys.exc_info())

      self.messages.task_done()
