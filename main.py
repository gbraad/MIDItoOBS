#!/usr/bin/env python

from __future__ import division
from websocket import WebSocketApp
from tinydb import TinyDB, Query
from sys import exit, stdout, argv
from os import path

import logging
import json
import mido

TEMPLATES = {
"ToggleSourceVisibility": """{
  "request-type": "GetSceneItemProperties",
  "message-id": "%d",
  "item": "%s"
}""",
"ReloadBrowserSource": """{
  "request-type": "GetSourceSettings",
  "message-id": "%d",
  "sourceName": "%s"
}"""
}

# QND: workaround to make multiple instances/devices work with a different configuration
configfile = "config.json"
if len(argv) > 1:
    configfile = argv[1]

SCRIPT_DIR = path.dirname(path.realpath(__file__))

def map_scale(inp, ista, isto, osta, osto):
    return osta + (osto - osta) * ((inp - ista) / (isto - ista))

def get_logger(name, level=logging.INFO):
    log_format = logging.Formatter('[%(asctime)s] (%(levelname)s) %(message)s')

    std_output = logging.StreamHandler(stdout)
    std_output.setFormatter(log_format)
    std_output.setLevel(level)

    file_output = logging.FileHandler(path.join(SCRIPT_DIR, "debug.log"))
    file_output.setFormatter(log_format)
    file_output.setLevel(level)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_output)
    logger.addHandler(std_output)
    return logger

class MidiHandler:
    # Initializes the handler class
    def __init__(self, config_path=configfile, ws_server="localhost", ws_port=4444):
        # Setting up logging first and foremost
        self.log = get_logger("midi_to_obs")

        # Internal service variables
        self._action_buffer = []
        self._action_counter = 2

        self.log.debug("Trying to load config file  from %s" % config_path)
        self.db = TinyDB(config_path, indent=4)
        result = self.db.search(Query().type.exists())
        if not result:
            self.log.critical("Config file %s doesn't exist or is damaged" % config_path)
            # ENOENT (No such file or directory)
            exit(2)

        self.log.info("Successfully parsed config file")
        port_name = str(result[0]["value"])

        self.log.debug("Retrieved MIDI port name `%s`" % port_name)
        del result

        try:
            self.log.debug("Attempting to open midi port")
            self.port = mido.open_input(name=port_name, callback=self.handle_midi_input)
        except:
            self.log.critical("The midi device %s is not connected or has a different name" % port_name)
            self.log.critical("Please plug the device in or run setup.py again and restart this script")
            # EIO 5 (Input/output error)
            exit(5)

        self.log.info("Successfully initialized midi port `%s`" % port_name)
        del port_name

        # Properly setting up a Websocket client
        self.log.debug("Attempting to connect to OBS using websocket protocol")
        self.obs_socket = WebSocketApp("ws://%s:%d" % (ws_server, ws_port))
        self.obs_socket.on_message = self.handle_obs_message
        self.obs_socket.on_error = self.handle_obs_error
        self.obs_socket.on_close = self.handle_obs_close
        self.obs_socket.on_open = self.handle_obs_open

    def handle_midi_input(self, message):
        self.log.debug("Received %s message from midi: %s" % (message.type, message))

        if message.type == "note_on":
            return self.handle_midi_button(message.type, message.note)

        # `program_change` messages can be only used as regular buttons since
        # they have no extra value, unlike faders (`control_change`)
        if message.type == "program_change":
            return self.handle_midi_button(message.type, message.program)

        if message.type == "control_change":
            return self.handle_midi_fader(message.control, message.value)


    def handle_midi_button(self, type, note):
        query = Query()
        results = self.db.search((query.msg_type == type) & (query.msgNoC == note))

        if not results:
            self.log.debug("Cound not find action for note %s", note)
            return

        for result in results:
            if self.send_action(result):
                break

    def handle_midi_fader(self, control, value):
        query = Query()
        results = self.db.search((query.msg_type == "control_change") & (query.msgNoC == control))

        if not results:
            self.log.debug("Cound not find action for fader %s", control)
            return

        for result in results:
            input_type = result["input_type"]
            action = result["action"]

            if input_type == "button":
                if value == 127 and not self.send_action(result):
                    continue
                break

            if input_type == "fader":
                command = result["cmd"]
                scaled = map_scale(value, 0, 127, result["scale_low"], result["scale_high"])

                if command == "SetSourcePosition" or command == "SetSourceScale":
                    self.obs_socket.send(action % scaled)
                    break

                # Super dirty hack but @AlexDash says that it works
                # @TODO: find an explanation _why_ it works
                if command == "SetVolume":
                    # Yes, this literally raises a float to a third degree
                    self.obs_socket.send(action % scaled**3)
                    break

                if command == "SetSourceRotation" or command == "SetTransitionDuration" or command == "SetSyncOffset":
                    self.obs_socket.send(action % int(scaled))
                    break

    def handle_obs_message(self, message):
        self.log.debug("Received new message from OBS")
        payload = json.loads(message)

        self.log.debug("Successfully parsed new message from OBS")

        if "error" in payload:
            self.log.error("OBS returned error: %s" % payload["error"])
            return

        message_id = payload["message-id"]

        self.log.debug("Looking for action with message id `%s`" % message_id)
        for action in self._action_buffer:
            (buffered_id, template, kind) = action

            if buffered_id != int(payload["message-id"]):
                continue

            del buffered_id
            self.log.info("Action `%s` was requested by OBS" % kind)

            if kind == "ToggleSourceVisibility":
                # Dear lain, I so miss decent ternary operators...
                invisible = "false" if payload["visible"] else "true"
                self.obs_socket.send(template % invisible)
            elif kind == "ReloadBrowserSource":
                source = payload["sourceSettings"]["url"]
                target = source[0:-1] if source[-1] == '#' else source + '#'
                self.obs_socket.send(template % target)

            self.log.debug("Removing action with message id %s from buffer" % message_id)
            self._action_buffer.remove(action)
            break

    def handle_obs_error(self, ws, error=None):
        # Protection against potential inconsistencies in `inspect.ismethod`
        if error is None and isinstance(ws, BaseException):
            error = ws

        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            self.log.info("Keyboard interrupt received, gracefully exiting...")
            self.close(teardown=True)
        else:
            self.log.error("Websocket error: %" % str(error))

    def handle_obs_close(self, ws):
        self.log.error("OBS has disconnected, timed out or isn't running")
        self.log.error("Please reopen OBS and restart the script")

    def handle_obs_open(self, ws):
        self.log.info("Successfully connected to OBS")

    def send_action(self, action_request):
        action = action_request.get("action")
        if not action:
            # @NOTE: this potentionally should never happen but you never know
            self.log.error("No action supplied in current request")
            return False

        request = action_request.get("request")
        if not request:
            self.log.debug("No request body for action %s, sending action" % action)
            self.obs_socket.send(action)
            # Success, breaking the loop
            return True

        template = TEMPLATES.get(request)
        if not template:
            self.log.error("Missing template for request %s" % request)
            # Keep searching
            return False

        target = action_request.get("target")
        if not target:
            self.log.error("Missing target in %s request for %s action" % (request, action))
            # Keep searching
            return False

        self._action_buffer.append([self._action_counter, action, request])
        self.obs_socket.send(template % (self._action_counter, target))
        self._action_counter += 1

        # Explicit return is necessary here to avoid extra searching
        return True

    def start(self):
        self.log.info("Connecting to OBS...")
        self.obs_socket.run_forever()

    def close(self, teardown=False):
        self.log.debug("Attempting to close midi port")
        self.port.close()

        self.log.info("Midi connection has been closed successfully")

        # If close is requested during keyboard interrupt, let the websocket
        # client tear itself down and make a clean exit
        if not teardown:
            self.log.debug("Attempting to close OBS connection")
            self.obs_socket.close()

            self.log.info("OBS connection has been closed successfully")

        self.log.debug("Attempting to close TinyDB instance on config file")
        self.db.close()

        self.log.info("Config file has been successfully released")

    def __end__(self):
        self.log.info("Exiting script...")
        self.close()

if __name__ == "__main__":
    handler = MidiHandler()
    handler.start()
