import argparse
import asyncio
import logging, json, threading, sys, subprocess, os, time
from urllib.parse import urlparse

import requests, websockets, numpy

from av import VideoFrame
from enum import Enum

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, VideoStreamTrack
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from aiortc.contrib.media import MediaBlackhole, MediaRecorder, MediaPlayer

logger = logging.getLogger('xirsysapp')

def create_rectangle(width, height, color):
    data_bgr = numpy.zeros((height, width, 3), numpy.uint8)
    data_bgr[:, :] = color
    return data_bgr

class FlagVideoStreamTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()  # don't forget this!
        self.data_bgr = numpy.hstack([
            create_rectangle(width=213, height=480, color=(255, 0, 0)),      # blue
            create_rectangle(width=214, height=480, color=(255, 255, 255)),  # white
            create_rectangle(width=213, height=480, color=(0, 0, 255)),      # red
        ])

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        frame = VideoFrame.from_ndarray(self.data_bgr, format='bgr24')
        frame.pts = pts
        frame.time_base = time_base
        return frame

class PeerConnection():

    class PcState(Enum):
        # no ice host including credential information is retrieved
        # note that it has an expiration, 30 seconds by default, see more below
        # https://docs.xirsys.com/?pg=api-turn#managing-expiring-credentials
        # this duration of an expiration is set by _ping_interval
        ICE_NOT_READY = 1

        # ice host information was retrieved
        # start gathering before the retrieved credential expires
        ICE_READY = 2

        ICING = 3
        CONNECTING = 4
        CONNECTED = 5
        DISCONNECTED = 6
        TERMINATED = 7

    def __init__(self, user_name, xirsys_url, channel_name, video_file, keep_alive_interval):
        self._user = user_name
        self._xirsys_url = xirsys_url
        self._channel_name = channel_name
        self._video_file = video_file
        self._keep_alive_interval = keep_alive_interval
        self._state = self.PcState.ICE_NOT_READY

        self._socket = None
        self._ice_state = None
        self._sender = None
        self._caller = None

    @property
    def state(self):
        return self._state
    
    @state.setter
    def state(self, value):
        logger.debug('state changed from {} to {}'.format(self._state, value))
        self._state = value

    async def get_wsurl(self):

        wsurl = None

        # 1. getting a temp token
        url = "{}/gettoken.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'username': self._user, 'channel': self._channel_name})
        token = r.json()['v']
        logger.debug('successfully retrieved a token: \t{}'.format(token))

        # 2. getting a host, and check if it is a valid one
        tried = 0
        while wsurl is None and tried < 10:
            url = "{}/gethost.php".format(self._xirsys_url)
            r = requests.post(url, verify=False, data={'username': self._user, 'channel': self._channel_name})
            js_r = r.json()
            logger.debug('received js_host:\t{}'.format(js_r))
            wshost = js_r['v']
            logger.debug('successfuly retrieved a host: \t{}'.format(wshost))
            wsurl = '{}/v2/{}'.format(wshost, token)
            try:
                async with websockets.client.connect(wsurl) as websocket:
                    logger.debug('successfully connected to {}'.format(wsurl))
            except:
                logger.exception('failed to connect to the host:\t{}'.format(wshost))
                wsurl = None
                tried += 1
        if wsurl is None:
            raise Exception('failed to get a valid host for the signaling')

        return wsurl

    async def doIce(self):
        
        # 1. get ice servers
        url = "{}/getice.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'channel': self._channel_name, 'expire': self._keep_alive_interval})
        ice_servers = r.json()['v']
        logger.debug('successfully retrieved ice hosts: \t{}'.format(ice_servers))

        # 2. peer connection
        config = self.generate_rtc_configuration(ice_servers)
        self._pc = RTCPeerConnection(config);

        # 3. add track event callback
        logger.debug('preparing black hole media as a sink')
        self._recorder = MediaBlackhole()
        @self._pc.on('track')
        def on_track(track):
            logger.debug('received track, adding...')
            if track.kind == 'video':
                self._recorder.addTrack(track)
                logger.debug('added track')
            else:
                logger.debug('can not add a track of {}'.format(track.kind))

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

    async def run(self):

        while not self._state == self.PcState.TERMINATED:

            try:

                if self._state == self.PcState.ICE_NOT_READY:
                    await self.doIce()

                #1. wait for a message over signaling
                logger.info('starting signaling...')
                await self.start_signaling()

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
                    break

    async def start_signaling(self):

        logger.debug('connecting with a keep alive interval = {}...'.format(self._keep_alive_interval))

        signaling_started = time.time()

        # disable the ping pong exchange, which causes the xirsys websocket to send a close request
        # https://websockets.readthedocs.io/en/stable/api.html#websockets.protocol.WebSocketCommonProtocol
        async with websockets.client.connect(await self.get_wsurl(), ping_interval=None) as websocket:

            while not self._state == self.PcState.TERMINATED and websocket.open:

                try:

                    await self.receive_message(websocket)

                except asyncio.TimeoutError:
                    elapsed = time.time() - signaling_started
                    logger.debug('stopping to receive a message, due to a regular break every 1 second...')

                    if self.state == self.PcState.DISCONNECTED:
                        logger.info('found the current state is closed, terminating the signaling...')
                        break

                    if elapsed > self._keep_alive_interval and self.state == self.PcState.ICE_READY:
                        logger.info('{} already passed, the ice credential is expired, renewing...'.format(self._keep_alive_interval))
                        break

                except websockets.exceptions.ConnectionClosed:
                    logger.error('websocket connection closed')
                    self.state = self.PcState.TERMINATED

        logging.debug('finished signaling')

    async def receive_message(self, websocket):

        message = await asyncio.wait_for(websocket.recv(), 1.0)

        logger.debug('received message over websocket: \t{}'.format(message))

        data = json.loads(message)
        msg_objective = data['m']['o']

        if msg_objective == 'peers':

            logger.debug('received peers notification')

        elif msg_objective == 'peer_connected':

            logger.debug('received a peer connected')
            joined = data['m']['f'].split('/')[-1];
            logger.info('{} joined'.format(joined))

        elif msg_objective == 'peer_removed':

            left = data['m']['f'].split('/')[-1];
            logger.info('{} has left'.format(left))

        elif msg_objective == 'message':

            msg = data['p']['msg']

            if 'type' in msg:

                msg_type = msg['type']

                if msg_type == 'offer':

                    logger.info('received an offer')
                    self.state = self.PcState.ICING
                    await self.make_answer(websocket, data)

                elif msg_type == 'answer':

                    logger.error('received an answer, but which should not happen')

                elif msg_type == 'candidate':

                    message = data['p']['msg']
                    logger.debug('received a candidate\t{}'.format(message))
                    candidate = candidate_from_sdp(message['candidate'].split(':', 1)[1])
                    candidate.sdpMid = message['sdpMid']
                    candidate.spdMLineIndex = message['sdpMLineIndex']
                    logger.debug('adding a candidate:\t{}'.format(candidate))
                    self._pc.addIceCandidate(candidate)

                elif msg_type == 'action':

                    if 'internal' in msg:

                        code = msg['code']

                        if code == 'rtc.p2p.close':

                            logger.info('the call was closed')

                        elif code == 'rtc.p2p.deny':

                            logger.info('the call was denied')

                else:

                    logger.warning('unknown message type: {}'.format(msg_type))

            else:

                logger.info('received a command message:\t{}'.format(msg))

                await self.execute_and_send(websocket, msg)

        else:

            logger.warning('unknown message objective: {}'.format(msg_objective))

    async def execute_and_send(self, websocket, message):

        logger.debug('executing {}'.format(message))

        output = None
        try:
            args = message.split(' ')
            output = subprocess.check_output(args)
            logger.debug('binary output: {}'.format(output))
            output = output.decode('ascii')
            logger.debug('text output: {}'.format(output))
            output = output.replace('\n', '<BR>')
            logger.debug('html output: {}'.format(output))
        except:
            output = 'can not execute {}'.format(message)

        js_output = {'t': 'u', 'm': {'f': "{}/{}".format(self._channel_name, self._user), 'o': 'message', 't': self._caller}, 'p': {'msg': '{}'.format(output)}};

        await self.send_message(websocket, json.dumps(js_output))

    async def send_message(self, websocket, message):
        logger.debug('sending the following message over websocket: \t{}'.format(message))
        await websocket.send(message)

    async def make_answer(self, websocket, data):

        peer = data['m']['f'].split('/')[-1];
        self._caller = peer
        logger.info('making an answer to {}'.format(self._caller))

        logger.debug('setting a remote description...')
        remote_desc = RTCSessionDescription(**data['p']['msg']);
        await self._pc.setRemoteDescription(remote_desc)

        self.setup_iceevents()

        # recorder start
        logger.debug('starting a black hole recorder...')
        await self._recorder.start()

        # add a video track
        logger.debug('adding a flag video stream as a track')
        self._sender = self._pc.addTrack(self.get_video_stream_track())
        self.setup_transport_events()

        # create offer
        logger.debug('creating offer, and then setting as a local description...')
        local_desc = await self._pc.createAnswer()
        await self._pc.setLocalDescription(local_desc)
        
        # send an answer
        logger.debug('sending an answer...')
        js_desc = {
            'sdp': self._pc.localDescription.sdp,
            'type': self._pc.localDescription.type
        }
        js_answer = {'t': 'u', 'm': {'f': "{}/{}".format(self._channel_name, self._user), 'o': 'message', 't': self._caller}, 'p': {'msg':js_desc}};
        await self.send_message(websocket, json.dumps(js_answer))

    def get_video_stream_track(self):

        logger.debug('getting a video stream track with {}'.format(self._video_file))
        stream_track = None

        try:
            player = MediaPlayer(self._video_file)
            stream_track = player.video
        except Exception as e:
            stream_track = FlagVideoStreamTrack()

        logger.debug('got a video stream track as {}'.format(stream_track))

        return stream_track

    def setup_iceevents(self):

        @self._pc.on('icegatheringstatechange')
        def on_icegatheringstatechange():
            logger.debug('iceGatheringState changed to {}'.format(self._pc.iceGatheringState))

        @self._pc.on('iceconnectionstatechange')
        def on_iceconnectionstatechange():
            logger.debug('iceConnectionState changed to {}'.format(self._pc.iceConnectionState))
            if self._pc.iceConnectionState == 'completed':
                self.state = self.PcState.CONNECTING
                logger.debug('current state = {}'.format(self._state))

    def setup_transport_events(self):
        @self._sender.transport.on('statechange')
        async def on_dtlsstatechanged():
            logger.debug('dtls state changed to {}'.format(self._sender.transport.state))
            if self._sender.transport.state == 'closed':
                self.state = self.PcState.DISCONNECTED

    async def cleanup(self):

        # do some cleanups
        await self.close()

        if not self._state == self.PcState.TERMINATED:
            self.state = self.PcState.ICE_NOT_READY

        return True

    async def close(self):

        await self._recorder.stop()
        await self._pc.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='xirsys python cli with aiortc')
    parser.add_argument('xirsys_url', help='an url prefix where getice.php, gethost.php, and gettoken.php from the official getting started guide are located. e.g. https://your.domain.com/xirsys')
    parser.add_argument('user_name', help='a user name for a signaling')
    parser.add_argument('--channel_name', '-c', default='sampleAppChannel', help='a xirsys channel name')
    parser.add_argument('--video_file', '-f', help='a file path to play')
    parser.add_argument('--keep_alive_interval', '-i', type=int, default=20, help='the duration of a keep alive interval')
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

    logger.info("launching a program with args = {}".format(args))

    conn = PeerConnection(args.user_name, args.xirsys_url, args.channel_name, args.video_file, args.keep_alive_interval)

    asyncio.run(conn.run())

    logger.info('finished running xirsys cli')
