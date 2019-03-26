import argparse
import asyncio
import logging, json, threading, sys
import requests, websockets
import numpy

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
        ICE_READY = 1
        ICE_NOT_READY = 2
        ICING = 3
        CONNECTING = 4
        CONNECTED = 5
        DISCONNECTED = 6
        TERMINATED = 7

    def __init__(self, user_name, xirsys_url, channel_name, video_file, disable_ping_interval):
        self._user = user_name
        self._xirsys_url = xirsys_url
        self._channel_name = channel_name
        self._video_file = video_file
        self._disable_ping_interval = disable_ping_interval
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
        r = requests.post(url, verify=False, data={'channel': self._channel_name})
        ice_servers = r.json()['v']
        logger.debug('successfully retrieved ice hosts: \t{}'.format(ice_servers))

        # 2. getting a temp token
        url = "{}/gettoken.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'username': self._user, 'channel': self._channel_name})
        token = r.json()['v']
        logger.debug('successfully retrieved a token: \t{}'.format(token))

        # 3. getting a host
        url = "{}/gethost.php".format(self._xirsys_url)
        r = requests.post(url, verify=False, data={'username': self._user, 'channel': self._channel_name})
        wshost = r.json()['v']
        logger.debug('successfuly retrieved a host: \t{}'.format(wshost))

        # 4. peer connection
        self._wsurl = '{}/v2/{}'.format(wshost, token)
        config = self.generate_rtc_configuration(ice_servers)
        self._pc = RTCPeerConnection(config);

        # 5. add track event callback
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

    @property
    def wsurl(self):
        return self._wsurl

    async def run(self):

        while not self._state == self.PcState.TERMINATED:

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
                    break

    async def keep_signaling(self):

        logger.debug('the websocket url for the signaling is: \t{}'.format(self._wsurl))

        ping_interval = 20
        if self._disable_ping_interval:
            ping_interval = None

        logger.debug('connecting...')
        async with websockets.client.connect(self._wsurl, ping_interval=ping_interval) as websocket:

            while not self._state == self.PcState.TERMINATED and websocket.open:

                try:

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

                        if joined != self._user:

                            self.state = self.PcState.ICING

                    elif msg_objective == 'peer_removed':

                        left = data['m']['f'].split('/')[-1];
                        logger.info('{} has left'.format(left))

                    elif msg_objective == 'message':

                        msg_type = data['p']['msg']['type']

                        if msg_type == 'offer':

                            logger.info('received an offer')
                            self.state = self.PcState.ICING
                            await self.make_answer(websocket, data)

                        elif msg_type == 'answer':

                            logger.info('received an answer')

                            await self.accept_answer(data)

                        elif msg_type == 'candidate':

                            message = data['p']['msg']
                            logger.debug('received a candidate\t{}'.format(message))
                            candidate = candidate_from_sdp(message['candidate'].split(':', 1)[1])
                            candidate.sdpMid = message['sdpMid']
                            candidate.spdMLineIndex = message['sdpMLineIndex']
                            logger.debug('adding a candidate:\t{}'.format(candidate))
                            self._pc.addIceCandidate(candidate)

                        else:

                            logger.warn('unknown message type: {}'.format(msg_type))

                    else:

                        logger.warning('unknown message objective: {}'.format(msg_objective))

                except asyncio.TimeoutError:
                    logger.debug('regular timeout occured...')

                    #if self._state == self.PcState.CONNECTING:
                    #    logger.debug('ice got completed, closing the websocket connection...')
                    #    await websocket.close()
                    #    logger.debug('closed the websocket connection')

                except websockets.exceptions.ConnectionClosed:
                    logger.error('websocket connection closed')

        logging.debug('finished signaling')

    async def send_message(self, websocket, message):
        logger.debug('sending the following message over websocket: \t{}'.format(message))
        await websocket.send(message)

    async def make_answer(self, websocket, data):

        peer = data['m']['f'].split('/')[-1];
        logger.info('making an answer to {}'.format(peer))

        # datachannel event is emitted inside setRemoteDescription
        @self._pc.on('datachannel')
        def on_datachannel(channel):

            logger.debug('on datachannel')
            self._channel = channel

        logger.debug('setting a remote description...')
        remote_desc = RTCSessionDescription(**data['p']['msg']);
        await self._pc.setRemoteDescription(remote_desc)

        self.setup_iceevents()

        # recorder start
        logger.debug('starting a black hole recorder...')
        await self._recorder.start()

        # add a video track
        logger.debug('adding a flag video stream as a track')
        self._pc.addTrack(self.get_video_stream_track())

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
        js_answer = {'t': 'u', 'm': {'f': "{}/{}".format(self._channel_name, self._user), 'o': 'message', 't': peer}, 'p': {'msg':js_desc}};
        await self.send_message(websocket, json.dumps(js_answer))

    async def accept_answer(self, data):

        peer = data['m']['f'].split('/')[-1];
        logger.info('got an answer from {}'.format(peer))

        logger.debug('setting a remote description...')
        remote_desc = RTCSessionDescription(**data['p']['msg']);
        await self._pc.setRemoteDescription(remote_desc)

        # recorder start
        logger.debug('starting a black hole recorder...')
        await self._recorder.start()

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
    parser.add_argument('--disable_ping_interval', '-p', action='store_true', help='disable to send a ping at an interval')
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

    conn = PeerConnection(args.user_name, args.xirsys_url, args.channel_name, args.video_file, args.disable_ping_interval)

    asyncio.run(conn.run())

    logger.info('finished running xirsys cli')
