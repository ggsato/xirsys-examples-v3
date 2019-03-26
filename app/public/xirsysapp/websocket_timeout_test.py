import argparse
import asyncio
import logging, json, threading, sys
import requests, websockets

from enum import Enum

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

logger = logging.getLogger('xirsysapp')

class PeerConnection():

    class PcState(Enum):
        ICE_READY = 1
        ICE_NOT_READY = 2
        ICING = 3
        CONNECTING = 4
        CONNECTED = 5
        DISCONNECTED = 6
        TERMINATED = 7

    def __init__(self, user_name, xirsys_url, active_ping):
        self._user = user_name
        self._xirsys_url = xirsys_url
        self._active_ping = active_ping
        self._state = self.PcState.ICE_NOT_READY

        self._wsurl = None
        self._socket = None
        self._ice_state = None
        self._channel = None

    @property
    def state(self):
        return self._state
    
    @state.setter
    def state(self, value):
        logger.debug('state changed from {} to {}'.format(self._state, value))
        self._state = value

    def doIce(self):
        
        # 1. get ice servers
        url = "{}/getice.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'channel': 'sampleAppChannel'})
        ice_servers = r.json()['v']
        logger.debug('successfully retrieved ice hosts: \t{}'.format(ice_servers))

        # 2. getting a temp token
        url = "{}/gettoken.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'username': self._user, 'channel': 'sampleAppChannel'})
        token = r.json()['v']
        logger.debug('successfully retrieved a token: \t{}'.format(token))

        # 3. getting a host
        url = "{}/gethost.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'username': self._user, 'channel': 'sampleAppChannel'})
        wshost = r.json()['v']
        logger.debug('successfuly retrieved a host: \t{}'.format(wshost))

        # 4. peer connection
        self._wsurl = '{}/v2/{}'.format(wshost, token)
        config = self.generate_rtc_configuration(ice_servers)
        self._pc = RTCPeerConnection(config);

        # change the state
        self.state = self.PcState.ICE_READY

    def generate_rtc_configuration(self, ice_servers):

        rtc_ice_servers = []

        # url should be urls according the spec
        # https://www.w3.org/TR/webrtc/#dom-rtciceserver
        for ice_server in ice_servers['iceServers']:

            # copy to a property urls as a list
            ice_server['urls'] = [ice_server['url']]
            del ice_server['url']

            rtc_ice_server = RTCIceServer(**ice_server)
            logger.debug('adding an ice server:\t{}'.format(rtc_ice_server))

            rtc_ice_servers.append(rtc_ice_server)

        return RTCConfiguration(rtc_ice_servers)

    @property
    def wsurl(self):
        return self._wsurl

    async def run(self):

        try:

            if self._state == self.PcState.ICE_NOT_READY:
                self.doIce()

            #1. wait for a message over signaling
            logger.info('starting signaling...')
            await self.keep_signaling()

        except KeyboardInterrupt:

            logger.info('detected Ctrl+C, terminating...')
            self.state = self.PcState.TERMINATED

        except Exception:

            logger.exception('an unhandled exception, terminating...')
            self.state = self.PcState.TERMINATED

        finally:
            #3. cleanup, and set ready
            if not await self.cleanup():
                logger.error('cleanup failed, terminating...')

    async def keep_signaling(self):

        logger.debug('the websocket url for the signaling is: \t{}'.format(self._wsurl))

        logger.debug('connecting...')
        async with websockets.client.connect(self._wsurl) as websocket:

            while not self._state == self.PcState.TERMINATED and websocket.open:

                try:

                    message = await asyncio.wait_for(websocket.recv(), 1.0)

                    logger.debug('received message over websocket: \t{}'.format(message))

                except asyncio.TimeoutError:
                    logger.debug('a break at a regular interval occured...')

                    if self._active_ping:

                        pong_waiter = await websocket.ping('active-ping')
                        await pong_waiter

                except websockets.exceptions.ConnectionClosed:
                    logger.error('websocket connection closed')

        logging.debug('finished signaling')

    async def cleanup(self):

        # do some cleanups
        await self.close()

        if not self._state == self.PcState.TERMINATED:
            self.state = self.PcState.ICE_NOT_READY

        return True

    async def close(self):

        await self._pc.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='xirsys python cli with aiortc')
    parser.add_argument('xirsys_url', help='an url prefix where getice.php, gethost.php, and gettoken.php from the official getting started guide are located. e.g. https://your.domain.com/xirsys')
    parser.add_argument('user_name', help='a user name for a signaling')
    parser.add_argument('--active_ping', '-a', action='store_true', help='Send a ping message actively')
    parser.add_argument('--verbose', '-v', action='store_true', help='debug logging enabled if set')

    args = parser.parse_args()

    f = '%(asctime)s [%(thread)d] %(message)s'
    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format=f)

    if not args.xirsys_url.startswith('http'):
        logging.error('xirsysurl should starts with http(s), exitting...')
        sys.exit(1)

    logger.info("getting xirsys ice hosts and tokens as {} with {}".format(args.user_name, args.xirsys_url))

    conn = PeerConnection(args.user_name, args.xirsys_url, args.active_ping)

    asyncio.run(conn.run())

    logger.info('finished running xirsys cli')
