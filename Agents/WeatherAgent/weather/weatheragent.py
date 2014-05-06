# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright (c) 2013, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are those
# of the authors and should not be interpreted as representing official policies,
# either expressed or implied, of the FreeBSD Project.
#

# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization
# that has cooperated in the development of these materials, makes
# any warranty, express or implied, or assumes any legal liability
# or responsibility for the accuracy, completeness, or usefulness or
# any information, apparatus, product, software, or process disclosed,
# or represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does
# not necessarily constitute or imply its endorsement, recommendation,
# r favoring by the United States Government or any agency thereof,
# or Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830

#}}}


import sys
import signal
import threading
import urllib2
import requests
import json
import datetime
import zmq
import time
import logging

from volttron.platform.agent import BaseAgent, PublishMixin, periodic
from volttron.platform.agent import utils, matching
from volttron.platform.messaging import headers as headers_mod

from pkg_resources import resource_string

import settings

utils.setup_logging()
_log = logging.getLogger(__name__)

'''
******* In order for this agent to retrieve data from Weather Underground, you must
get a developer's key and put that into the config file.

http://www.wunderground.com/weather/api/

********
'''

publish_address = 'ipc:///tmp/volttron-platform-agent-publish'
subscribe_address = 'ipc:///tmp/volttron-platform-agent-subscribe'
topic_delim = '/'


temperature = ["temperature_string", "temp_f", "temp_c", "feelslike_c", "feelslike_f", "feelslike_string", "windchill_c", "windchill_f", "windchill_string", "heat_index_c", "heat_index_f", "heat_index_string"]
wind = ["wind_gust_kph", "wind_string", "wind_mph", "wind_dir", "wind_degrees", "wind_kph", "wind_gust_mph", "pressure_in"]
location = ["local_tz_long", "observation_location", "display_location", "station_id"]
time_topics = ["local_time_rfc822", "local_tz_short", "local_tz_offset", "local_epoch", "observation_time", "observation_time_rfc822", "observation_epoch"]
cloud_cover = ["weather", "solarradiation", "visibility_mi", "visibility_km", "UV"]
precipitation = ["dewpoint_string", "precip_today_string", "dewpoint_f", "dewpoint_c", "precip_today_metric", "precip_today_in", "precip_1hr_in", "precip_1hr_metric", "precip_1hr_string"]
pressure_humidity = ["pressure_trend", "pressure_mb", "relative_humidity"]

categories = {"temperature": temperature, "wind": wind, "location": location, "time": time_topics, "cloud_cover": cloud_cover, 'precipitation': precipitation, 'pressure_humidity': pressure_humidity}


class RequestCounter:
    def __init__(self, daily_threshold, minute_threshold):
        self.daily = 0
        self.date = datetime.datetime.today().date()
        self.per_minute_requests = []
        self.daily_threshold = daily_threshold
        self.minute_threshold = minute_threshold

    def request_available(self):
        now = datetime.datetime.today()
        if (now.date() - self.date).days < 1:
            if self.daily >= self.daily_threshold:
                return False
        else:
            self.date = now.date()
            self.daily = 0

        while len(self.per_minute_requests) > 0 and (now - self.per_minute_requests[-1]).seconds > 60:
            self.per_minute_requests.pop()

        if len(self.per_minute_requests) < self.minute_threshold:
            self.per_minute_requests.insert(0, now)
            self.daily += 1

        else:
            return False

        return True

    def store_state(self):
        pass


def WeatherAgent(config_path, **kwargs):
    config = utils.load_config(config_path)

    def get_config(name):
        try:
            value = kwargs.pop(name)
        except KeyError:
            return config.get(name, '')

    agent_id = get_config('agentid')
    poll_time = get_config('poll_time')
    zip_code = get_config("zip")
    key = get_config('key')

    state = ''
    country = ''
    city = ''
    region = state if state != "" else country
    city = city
    max_requests_per_day = get_config('daily_threshold')
    max_requests_per_minute = get_config('minute_threshold')
    headers = {headers_mod.FROM: agent_id}

    class Agent(PublishMixin, BaseAgent):
        """Agent for querying WeatherUndergrounds API"""

        def __init__(self, **kwargs):
            super(Agent, self).__init__(**kwargs)
            self.valid_data = False

        def setup(self):
            super(Agent, self).setup()

            self._keep_alive = True

            self.requestCounter = RequestCounter(max_requests_per_day, max_requests_per_minute)
            # TODO: get this information from configuration file instead

            baseUrl = "http://api.wunderground.com/api/" + (key if not key == '' else settings.KEY) + "/conditions/q/"

            self.requestUrl = baseUrl
            if(zip_code != ""):
                self.requestUrl += zip_code + ".json"
            elif self.region != "":
                self.requestUrl += region + "/" + city + ".json"
            else:
                # Error Need to handle this
                print "No location selected"

            #Do a one time push when we start up so we don't have to wait for the periodic
            self.timer(10, self.weather_push)

        def build_dictionary(self):
            weather_dict = {}
            for category in categories.keys():
                weather_dict[category] = {}
                weather_elements = categories[category]
                for element in weather_elements:
                    weather_dict[category][element] = self.observation[element]

            return weather_dict

        def publish_all(self):
            self.publish_subtopic(self.build_dictionary(), "weather")

        def publish_subtopic(self, publish_item, topic_prefix):
            #TODO: Update to use the new topic templates
            if type(publish_item) is dict:
                # Publish an "all" property, converting item to json

                headers[headers_mod.CONTENT_TYPE] = headers_mod.CONTENT_TYPE.JSON
                self.publish_json(topic_prefix + topic_delim + "all", headers, json.dumps(publish_item))

                # Loop over contents, call publish_subtopic on each
                for topic in publish_item.keys():
                    self.publish_subtopic(publish_item[topic], topic_prefix + topic_delim + topic)

            else:
                # Item is a scalar type, publish it as is
                headers[headers_mod.CONTENT_TYPE] = headers_mod.CONTENT_TYPE.PLAIN_TEXT
                self.publish(topic_prefix, headers, str(publish_item))

        @periodic(poll_time)
        def weather_push(self):
            self.request_data()
            if self.valid_data:
                self.publish_all()
            else:
                _log.error("Invalid data, not publishing")

        def request_data(self):
            if self.requestCounter.request_available():
                try:
                    r = requests.get(self.requestUrl)
                    r.raise_for_status()
                    parsed_json = r.json()

                    self.observation = parsed_json['current_observation']
                    self.observation = convert(self.observation)
                    self.valid_data = True
                except Exception as e:
                    _log.error(e)
                    self.valid_data = False
                #self.print_data()

            else:
                _log.warning("No requests available")

        def print_data(self):
            print "{0:*^40}".format(" ")
            for key in self.observation.keys():
                print "{0:>25}: {1}".format(key, self.observation[key])
            print "{0:*^40}".format(" ")

    Agent.__name__ = 'WeatherAgent'
    return Agent(**kwargs)


def convert(input):
    if isinstance(input, dict):
        return {convert(key): convert(value) for key, value in input.iteritems()}
    elif isinstance(input, list):
        return [convert(element) for element in input]
    elif isinstance(input, unicode):
        return input.encode('utf-8')
    else:
        return input


def main(argv=sys.argv):
    '''Main method called by the eggsecutable.'''
    utils.default_main(WeatherAgent,
                       description='Weather Underground agent',
                       argv=argv)


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
