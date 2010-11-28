#!/usr/bin/env python

############################################################################
# Transcoder for Amarok
#
# The only user servicable parts are the encode/decode (line 103) and the
# number of concurrent jobs to run (line 225)
#
# The optional module tagpy (http://news.tiker.net/software/tagpy) is used
# for tag information processing. This allows for writing tags into the
# transcoded files.
#
############################################################################
#
# Copyright (C) 2007 Daniel O'Connor. All rights reserved.
# Copyright (C) 2007 Jens Zurheide. All rights reserved.
# Copyright (C) 2008 Toby Dickenson. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
############################################################################

__version__ = "1.9"

import os
import sys
import signal
import logging
import select
import subprocess
import tempfile
from logging.handlers import RotatingFileHandler
import urllib
import urlparse
import time
from optparse import OptionParser
from sets import Set

try:
    import tagpy
except ImportError, e:
    tagpy = None

def main():
    parser = OptionParser()
    parser.add_option("--test",
                    action="store_true", dest="test", default=False,
                    help="run a test")
    (options, args) = parser.parse_args()

    initLog()
    signal.signal(signal.SIGINT, onStop)
    signal.signal(signal.SIGHUP, onStop)
    signal.signal(signal.SIGTERM, onStop)

    if options.test:
        quick_test()
    else:
        # Run normal application
        app = amaKode()
        try:
            app.run()
        except Exception:
            log.exception()

def quick_test():
    # Quick test case
    def reportJob(job):
        log.debug('FINISHED %r, errormsg=%s'%(job,job.errormsg))
        job.clean_up()
    q = QueueMgr(reportJob)
    j1 = TranscodeJob("file:test/test1.m4a", "ogg")
    q.add(j1)
    j2 = TranscodeJob("file:test/test2.mp3", "ogg")
    q.add(j2)
    while not q.isidle():
        q.poll()
        res = select.select([], [], [], 1)
    log.debug("jobs all done")



def get_tags(filename,ext):
    # list of mp4 extensions to use atomicparsley with 
    # (tagpy seems to silently die on mp4/m4a files)
    mp4_ext = ("mp4", "m4a")

    if ext in mp4_ext:
        if is_on_path('AtomicParsley'):
            return atomicparsleywrap(filename)
        else:
            notify_missing_package('AtomicParsley',ext,'atomicparsley','atomicparsley')
            return None
    else:
        if tagpy is not None:
            return tagpywrap(filename)
        else:
            notify_missing_package('TagPy',ext,'python-tagpy','tagpy')
            return None

class atomicparsleywrap(dict):
    textfields = ['album', 'artist', 'title', 'comment', 'genre']
    apfields =   ['alb',   'art',    'nam',   'cmt',     'gnre']
    numfields = ['year', 'track']
    allfields = textfields + numfields

    def __init__(self, filename):
        ap = subprocess.Popen(['AtomicParsley',filename,'-t'], stdout=subprocess.PIPE)
        for line in ap.stdout:
            fields = line.rstrip().split(None, 3)
            if len(fields)==4 and fields[0]=="Atom":
                for apfield,textfield in zip(self.apfields,self.textfields):
                    if fields[1].lower().find(apfield)!=-1:
                        self[textfield] = unicode(fields[3],'utf8')
                        break
                else:
                    if fields[1].lower().find('day')!=-1:
                        try: self['year'] = int(fields[3])
                        except ValueError: self['year'] = 0
                    elif fields[1].lower().find('trkn')!=-1:
                        try: self['track'] = int(fields[3].split()[0])
                        except ValueError: self['track'] = 0

class tagpywrap(dict):
    textfields = ['album', 'artist', 'title', 'comment', 'genre']
    numfields = ['year', 'track']
    allfields = textfields + numfields

    def __init__(self, filename):
        log.debug("Reading tags from "+filename)
        self.tagInfo = tagpy.FileRef(filename).tag()

        self['album'] = self.tagInfo.album.strip()
        self['artist'] = self.tagInfo.artist.strip()
        self['title'] = self.tagInfo.title.strip()
        self['comment'] = self.tagInfo.comment.strip()
        self['year'] = self.tagInfo.year
        self['genre'] = self.tagInfo.genre.strip()
        self['track'] = self.tagInfo.track
        for i in self.textfields:
            if (self[i] == ""):
                del self[i]

        for i in self.numfields:
            if (self[i] == 0):
                del self[i]

already_notified_missing_package = Set()
def notify_missing_package(name,ext,debian_package,rpm_package):
    if name in already_notified_missing_package:
        return
    already_notified_missing_package.add(name)
    if os.path.exists('/var/lib/apt'):
        # smells like a debian system, so use the debian package name
        package = debian_package
    else:
        package = rpm_package # rpm-based and gentoo seem to be mostly the same
    tagpymsg = 'Amakode can not find the '+name+' library. Please install the '+package+' package, '
    tagpymsg += 'otherwise any '+ext+' files copied to your media player will not have tags.'
    subprocess.call(['dcop','amarok','playlist','popupMessage',tagpymsg])

class QueueMgr(object):
    queuedjobs = []
    activejobs = []

    def __init__(self, callback = None):
        self.callback = callback

        self.maxjobs = number_of_processors()
        log.debug('Using %d concurrent jobs because it seems there are %d processors' % (self.maxjobs,self.maxjobs))

    def add(self, job):
        log.debug("Job added")
        self.queuedjobs.append(job)

    def poll(self):
        """ Poll active jobs and check if we should make a new job active """

        for j in self.activejobs:
            if j.isfinished():
                log.debug("job is done")
                self.activejobs.remove(j)
                if (self.callback != None):
                    self.callback(j)

        while len(self.queuedjobs) > 0 and len(self.activejobs) < self.maxjobs:
            newjob = self.queuedjobs.pop(0)
            newjob.start()
            self.activejobs.append(newjob)

    def isidle(self):
        """ Returns true if both queues are empty """
        return(len(self.queuedjobs) == 0 and len(self.activejobs) == 0)

class TranscodeJob(object):
    # Programs used to decode (to a wav stream)
    decode = {}
    decode["mp3"] = ["mpg123", "-w", "/dev/stdout", "-"]
    decode["ogg"] = ["ogg123", "-d", "wav", "-f", "-", "-"]
    # XXX: this is really fugly but faad refuses to read from a pipe
    decode["mp4"] = ["env", "MPLAYER_VERBOSE=-100", "mplayer", "-ao", "pcm:file=/dev/stdout", "-"]
    decode["m4a"] = decode["mp4"]
    decode["flac"] = ["flac", "-d", "-c", "-"]
    decode["wav"] = ["cat"]
    decode["mpc"] = ["mpcdec", "-", "-"]

    # Programs used to encode (from a wav stream)
    encode = {}
    encode["mp3"] = ["lame", "--abr", "128", "-", "-"]
    encode["ogg"] = ["oggenc", "-q", "2", "-"]
    encode["mp4"] = ["faac", "-wo", "/dev/stdout", "-"]
    encode["m4a"] = encode["mp4"]
    encode["wav"] = ["cat"]
    encode["mpc"] = ["mpcenc", "--silent", "--standard", "-", "-"]

    # XXX: can't encode flac - it's wav parser chokes on mpg123's output, it does work
    # OK if passed through sox but we can't do that. If you really want flac modify
    # the code & send me a diff or write a wrapper shell script :)
    #encode["flac"] = ["flac", "-c", "-"]

    # Options for output programs to store ID3 tag information
    tagopt = {}
    tagopt["mp3"] = { "album":"--tl", "artist":"--ta", "title":"--tt", "track":"--tn" }
    tagopt["ogg"] = { "album":"-l", "artist": "-a", "title":"-a", "track":"-N" }
    tagopt["mp4"] = { "album":"--album", "artist":"--artist", "title":"--title", "track":"--track" }
    #tagopt["flac"] = { "album":"-Talbum=%s", "artist":"-Tartist=%s", "title":"-Ttitle=%s", "track":"-Ttracknumber=%s" }
    tagopt["mpc"] = { "album":"--album", "artist":"--artist", "title":"--title", "track":"--track" }

    def __init__(self, _inurl, _tofmt):
        self.errormsg = None
        log.debug("Creating job")
        self.inurl = _inurl
        self.tofmt = _tofmt.lower()
        self.inext = os.path.splitext(self.inurl)[1].lstrip('.').lower()
        self._files_to_clean_up_on_success = []
        self._files_to_clean_up_on_error = []

    def start(self):
        log.debug("Starting job")
        try:
            self.check_codecs()
            self.prepare_files()
            self.start_codec()
        except:
            log.exception("Failed to start")
            self.errormsg = str(sys.exc_info()[1])

    def check_codecs(self):
        try:
            decoder = self.decode[self.inext]
        except KeyError:
            log.debug("unable to decode " + self.inext)
            raise KeyError("no available decoder for "+self.inext)
        else:
            decoder = decoder[0]
            if is_on_path(decoder):
                log.debug("can decode with " + decoder)
            else:
                raise KeyError("It seems you do not have "+decoder+" installed, which is needed to decode "+self.inext+" files. Please install it using your package manager.")

        try:
            encoder = self.encode[self.tofmt]
        except KeyError:
            log.debug("unable to encode " + self.tofmt)
            raise KeyError("no available encoder for "+self.tofmt)
        else:
            encoder = encoder[0]
            if is_on_path(encoder):
                log.debug("can encode with " + encoder)
            else:
                raise KeyError("It seems you do not have "+encoder+" installed, which is needed to encode "+self.tofmt+" files. Please install it using your package manager.")

    def prepare_files(self):
        if urlparse.urlsplit(self.inurl)[0]=='file':
            self.infname = urllib.url2pathname(urlparse.urlsplit(self.inurl)[2])
            self.infd = open(self.infname)
        else:
            # not a file url. download it.
            source = urllib.urlopen(self.inurl)
            self.infd, self.infname = tempfile.mkstemp(prefix="transcode-in-", suffix="." + self.inext)
            self._files_to_clean_up_on_success.append((self.infd, self.infname))
            self._files_to_clean_up_on_error.append((self.infd, self.infname))
            while True:
                chunk = source.read(1024*64)
                if not chunk:
                    break
                os.write(self.infd,chunk)
            os.lseek(self.infd,0,0)

        self.outfd, self.outfname = tempfile.mkstemp(prefix="transcode-out-", suffix="." + self.tofmt)
        self._files_to_clean_up_on_error.append((self.outfd, self.outfname))

        self.errfh, self.errfname = tempfile.mkstemp(prefix="transcode-", suffix=".log")
        self.outurl = urlparse.urlunsplit(["file", None, self.outfname, None, None])
        self._files_to_clean_up_on_success.append((self.errfh, self.errfname))
        log.debug("Reading from " + self.infname + " (" + self.inurl + ")")
        log.debug("Outputting to " + self.outfname + " (" + self.outurl + ")")
        log.debug("Errors to " + self.errfname)

    def start_codec(self):
        # assemble command line for encoder
        encoder = []

        encoder += self.encode[self.tofmt]

        if self.tofmt in self.tagopt:
            taginfo = get_tags(self.infname,self.inext)
            if taginfo:
                for f in taginfo.allfields:
                    if f in taginfo and f in self.tagopt[self.tofmt]:
                        inf = taginfo[f]
                        opt = self.tagopt[self.tofmt][f]
                        log.debug("  %s = %s %s" % (f, opt, inf))
                        # If we have a substitution, make it. If
                        # not append the info as a separate
                        # arg. Note that the tag options are
                        # passed in as the second option because a
                        # lot of programs don't parse options
                        # after their file list.
                        if type(inf)==type(u''):
                            inf = inf.encode('utf8')
                        else:
                            inf = str(inf)
                        if '%s' in opt:
                            opt = opt.replace('%s', inf)
                            encoder.insert(1, opt)
                        else:
                            encoder.insert(1, opt)
                            encoder.insert(2, inf)

        log.debug("decoder -> " + str(self.decode[self.inext]))
        log.debug("encoder -> " + str(encoder))
        self.decoder = subprocess.Popen(self.decode[self.inext], stdin=self.infd, stdout=subprocess.PIPE, stderr=self.errfh)
        self.encoder = subprocess.Popen(encoder, stdin=self.decoder.stdout, stdout=self.outfd, stderr=self.errfh)
        log.debug("Processes connected")

    def isfinished(self):
        if self.errormsg is not None: # redundant?
            return True

        rtn = self.encoder.poll()
        if rtn == None:
            return False

        if rtn == 0:
            self.errormsg = None
        else:
            log.debug("error in transcode, please review " + self.errfname)
            self.errormsg = "Unable to transcode\n\n"+open(self.errfname).read()

        return True

    def clean_up(self):
        for (fd, filename) in self._files_to_clean_up_on_error,
                self._files_to_clean_up_on_success:
            os.close(fd)
        if self.errormsg:
            list_of_files = self._files_to_clean_up_on_error
        else:
            list_of_files = self._files_to_clean_up_on_success
        for (fd, filename) in list_of_files:
            log.debug("deleting "+filename)
            try:
                os.unlink(filename)
            except:
                pass

############################################################################
# amaKode
############################################################################
class amaKode(object):
    """ The main application"""

    def __init__(self):
        """ Main loop waits for something to do then does it """
        self.last_message_time = 0
        self.queue = QueueMgr(callback = self.job_finished)

    def run(self):
        log.debug("Started.")
        while True:
            # Check for finished jobs, etc
            self.queue.poll()
            # Check if there's anything waiting on stdin
            res = select.select([sys.stdin.fileno()], [], [], 
                    not self.queue.isidle() and 0.1 or None)
            if (sys.stdin.fileno() in res[0]):
                # Let's hope we got a whole line or we stall here
                line = sys.stdin.readline()
                if line:
                    self.customEvent(line)
                else:
                    log.debug("exiting...")
                    break

    def customEvent(self, string):
        """ Handles notifications """

        #log.debug("Received notification: " + str(string))

        if string.startswith("transcode"):
            self.transcode(str(string))

        if string.startswith("quit"):
            self.quit()

        if string.startswith("configure"):
            self.configure()

    def transcode(self, line):
        """ Called when requested to transcode a track """
        args = line.split()
        if (len(args) != 3):
            log.debug("Invalid transcode command")
            return

        log.debug("transcoding " + args[1] + " to " + args[2])

        newjob = TranscodeJob(args[1], args[2])
        self.queue.add(newjob)

    def job_finished(self,job):
        self.notify_amarok_that_job_is_finished(job)
        job.clean_up()

    def notify_amarok_that_job_is_finished(self, job):
        """ Report to amarok that the job is done """
        if job.errormsg:
            log.debug("Job " + job.inurl + " failed - " + job.errormsg)
            subprocess.call(['dcop','amarok','mediabrowser','transcodingFinished',job.inurl,''])

            # get Amarok to pop up our error message, but not too frequently
            now = time.time()
            if now>self.last_message_time+10:
                subprocess.call(['dcop','amarok','playlist','popupMessage',job.errormsg])
                last_message_time = now
        else:
            log.debug("Job " + job.inurl + " completed successfully")
            subprocess.call(['dcop','amarok','mediabrowser','transcodingFinished',job.inurl,job.outurl])

    def quit(self):
        log.debug("quitting")
        sys.exit()

    def configure(self):
        os.system("kdialog --title Amakode --msgbox \"Amakode can be configured using Amarok's media device settings.\"")

############################################################################

def is_on_path(name):
    path = os.environ.get('PATH')
    if not path:
        path = os.defpath
    for directory in path.split(os.pathsep):
        candidate = os.path.join(directory,name)
        if os.path.exists(candidate):
            # well, its present. We havent checked whether it is excecutable and there are
            # still plenty of other things that could go wrong when excecuting it.
            return True

def onStop(signum, stackframe):
    """ Called when script is stopped by user """
    log.debug("signalled exit")
    sys.exit()

def number_of_processors():
    try:
        return os.sysconf('SC_NPROCESSORS_ONLN')
    except:
        return 1

def initLog():
    # Init our logging
    global log
    log = logging.getLogger("amaKode")
    # Default to warts and all logging
    log.setLevel(logging.DEBUG)

    # Log to this file
    logfile = logging.handlers.RotatingFileHandler(filename = os.path.join(os.getcwd(),'amakode.log'),
                                                   maxBytes = 10000, backupCount = 3)

    # And stderr
    logstderr = logging.StreamHandler()

    # Format it nicely
    formatter = logging.Formatter("[%(name)s] %(message)s")

    # Glue it all together
    logfile.setFormatter(formatter)
    logstderr.setFormatter(formatter)
    log.addHandler(logfile)
    log.addHandler(logstderr)
    return(log)



if __name__=='__main__':
    main()
