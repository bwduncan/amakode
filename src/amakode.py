#!/usr/bin/env python

############################################################################
# Transcoder for Amarok
#
# Depends on: Python 2.4
#             tagpy (optional)
#
# Thanks to jeffpc@josefsipek.net, jens.zurheide@gmx.de,
# tcuya from kde-apps.org, and kontakt@lombacher.net for patches & bug
# reports.
#
# The only user servicable parts are the encode/decode (line 103) and the
# number of concurrent jobs to run (line 225)
#
# The optional module tagpy (http://news.tiker.net/software/tagpy) is used 
# for tag information processing. This allows for writing tags into the 
# transcoded files.
#
# Mercurial repo available at http://www.dons.net.au/~darius/hgwebdir.cgi/amakode/
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

__version__ = "1.3"

import ConfigParser
import os
import sys
import string
import signal
import logging
import select
import subprocess
import tempfile
from logging.handlers import RotatingFileHandler
import urllib
import urlparse
import re

try:
    import tagpy
    have_tagpy = True
except ImportError, e:
    have_tagpy = False
    
class tagpywrap(dict):
    textfields = ['album', 'artist', 'title', 'comment', 'genre']
    numfields = ['year', 'track']
    allfields = textfields + numfields
    
    def __init__(self, url):
        f = urllib.urlopen(url)

        self.tagInfo = tagpy.FileRef(f.fp.name).tag()
        f.close()
    
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

class QueueMgr(object):
    queuedjobs = []
    activejobs = []
    
    def __init__(self, callback = None, maxjobs = 2):
        self.callback = callback
        self.maxjobs = maxjobs
        pass
    
    def add(self, job):
        log.debug("Job added")
        self.queuedjobs.append(job)
    
    def poll(self):
        """ Poll active jobs and check if we should make a new job active """
        if (len(self.activejobs) == 0):
            needajob = True
        else:
            needajob = False

        for j in self.activejobs:
            if j.isfinished():
                log.debug("job is done")
                needajob = True
                self.activejobs.remove(j)
                if (self.callback != None):
                    self.callback(j)

        if needajob:
            #log.debug("Number of queued jobs = " + str(len(self.queuedjobs)) + ", number of active jobs = " + str(len(self.activejobs)))
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

    # Programs used to encode (from a wav stream)
    encode = {}
    encode["mp3"] = ["lame", "--abr", "128", "-", "-"]
    encode["ogg"] = ["oggenc", "-q", "2", "-"]
    encode["mp4"] = ["faac", "-wo", "/dev/stdout", "-"]
    encode["m4a"] = encode["mp4"]
    encode["wav"] = ["cat"]

    # XXX: can't encode flac - it's wav parser chokes on mpg123's output, it does work
    # OK if passed through sox but we can't do that. If you really want flac modify
    # the code & send me a diff or write a wrapper shell script :)
    #encode["flac"] = ["flac", "-c", "-"]

    # Options for output programs to store ID3 tag information
    tagopt = {}
    tagopt["mp3"] = { "album" : "--tl", "artist" : "--ta", "title" : "--tt", "track" : "--tn" }
    tagopt["ogg"] = { "album" : "-l", "artist" :  "-a", "title" : "-a", "track" : "-N" }
    tagopt["mp4"] = { "album" : "--album", "artist" : "--artist", "title" : "--title", "track" : "--track" }
    #tagopt["flac"] = { "album" : "-Talbum=%s", "artist" : "-Tartist=%s", "title" : "-Ttitle=%s", "track" : "-Ttracknumber=%s" }

    def __init__(self, _inurl, _tofmt):
        self.errormsg = None
        log.debug("Creating job")
        self.inurl = _inurl
        self.tofmt = string.lower(_tofmt)
        self.inext = string.lower(string.rsplit(self.inurl, ".", 1)[1])
        if (self.inext in self.decode):
            log.debug("can decode with " + str(self.decode[self.inext]))
        else:
            log.debug("unable to decode " + self.inext)
            raise KeyError("no available decoder")
        
        if (self.tofmt in self.encode):
            log.debug("can encode with " + str(self.encode[self.tofmt]))
        else:
            log.debug("unable to encode " + self.tofmt)
            raise KeyError("no available encoder")

    def start(self):
        log.debug("Starting job")
        try:
            self.inputfile = urllib.urlopen(self.inurl)
            self.outfd, self.outfname = tempfile.mkstemp(prefix="transcode-", suffix="." + self.tofmt)
            #self.outfname = string.join(string.rsplit(self.inurl, ".")[:-1] + [self.tofmt], ".")

            self.errfh, self.errfname = tempfile.mkstemp(prefix="transcode-", suffix=".log")
            self.outurl = urlparse.urlunsplit(["file", None, self.outfname, None, None])
            log.debug("Outputting to " + self.outfname + " " + self.outurl + ")")
            log.debug("Errors to " + self.errfname)
            
            # assemble command line for encoder
            encoder = []
            encoder += self.encode[self.tofmt]
            
            try:
                if (have_tagpy and self.tofmt in self.tagopt):
                    taginfo = tagpywrap(self.inurl)
                    for f in taginfo.allfields:
                        if (f in taginfo and f in self.tagopt[self.tofmt]):
                            inf = taginfo[f]
                            opt = self.tagopt[self.tofmt][f]
                            log.debug("  %s = %s %s" % (f, opt, inf))
                            # If we have a substitution, make it. If
                            # not append the info as a separate
                            # arg. Note that the tag options are
                            # passed in as the second option because a
                            # lot of programs don't parse options
                            # after their file list.
                            if ('%s' in opt):
                                opt = opt.replace('%s', inf)
                                encoder.insert(1, opt)
                            else:
                                encoder.insert(1, opt)
                                encoder.insert(2, str(inf))
            finally:
                pass

            log.debug("decoder -> " + str(self.decode[self.inext]))
            log.debug("encoder -> " + str(encoder))
            self.decoder = subprocess.Popen(self.decode[self.inext], stdin=self.inputfile, stdout=subprocess.PIPE, stderr=self.errfh)
            self.encoder = subprocess.Popen(encoder, stdin=self.decoder.stdout, stdout=self.outfd, stderr=self.errfh)
            log.debug("Processes connected")
        except Exception, e:
            log.debug("Failed to start - " + str(e))
            self.errormsg = str(e)
            try:
                os.unlink(self.outfname)
            except:
                pass
        
    def isfinished(self):
        if (self.errormsg != None):
            return(True)

        rtn = self.encoder.poll()
        if (rtn == None):
            return(False)

        if (rtn == 0):
            os.unlink(self.errfname)
            self.errormsg = None
        else:
            log.debug("error in transcode, please review " + self.errfname)
            self.errormsg = "Unable to transcode, please review " + self.errfname
            try:
                os.unlink(self.outfname)
            except:
                pass
            
        return(True)
    
############################################################################
# amaKode
############################################################################
class amaKode(object):
    """ The main application"""

    def __init__(self, args):
        """ Main loop waits for something to do then does it """
        log.debug("Started.")
        if (have_tagpy):
            log.debug("Using tagpy")
        else:
            log.debug("Warning: tagpy is unavailable")
            
        self.readSettings()

        self.queue = QueueMgr(callback = self.notify, maxjobs = 1)
        
        while True:
            # Check for finished jobs, etc
            self.queue.poll()
            # Check if there's anything waiting on stdin
            res = select.select([sys.stdin.fileno()], [], [], 0.1)
            if (sys.stdin.fileno() in res[0]):
                # Let's hope we got a whole line or we stall here
                line = sys.stdin.readline()
                if line:
                    self.customEvent(line)
                else:
                    break
            
    def readSettings(self):
        """ Reads settings from configuration file """

        try:
            foovar = config.get("General", "foo")

        except:
            log.debug("No config file found, using defaults.")

    def customEvent(self, string):
        """ Handles notifications """

        #log.debug("Received notification: " + str(string))

        if string.find("transcode") != -1:
            self.transcode(str(string))

        if string.find("quit") != -1:
            self.quit()

    def transcode(self, line):
        """ Called when requested to transcode a track """
        args = string.split(line)
        if (len(args) != 3):
            log.debug("Invalid transcode command")
            return

        log.debug("transcoding " + args[1] + " to " + args[2])
        try:
            newjob = TranscodeJob(args[1], args[2])
        except:
            log.debug("Can't create transcoding job")
            os.system("dcop amarok mediabrowser transcodingFinished " + re.escape(args[1]) + "\"\"")
            
        self.queue.add(newjob)

    def notify(self, job):
        """ Report to amarok that the job is done """
        if (job.errormsg == None):
            log.debug("Job " + job.inurl + " completed successfully")
            os.system("dcop amarok mediabrowser transcodingFinished " + re.escape(job.inurl) + " " + re.escape(job.outurl))
        else:
            log.debug("Job " + job.inurl + " failed - " + job.errormsg)
            os.system("dcop amarok mediabrowser transcodingFinished " + re.escape(job.inurl) + "\"\"")

    def quit(self):
        log.debug("quitting")
        sys.exit()

############################################################################

def debug(message):
    """ Prints debug message to stdout """
    log.debug(message)

def onStop(signum, stackframe):
    """ Called when script is stopped by user """
    log.debug("signalled exit")
    sys.exit()

def initLog():
    # Init our logging
    global log
    log = logging.getLogger("amaKode")
    # Default to warts and all logging
    log.setLevel(logging.DEBUG)

    # Log to this file
    logfile = logging.handlers.RotatingFileHandler(filename = "/tmp/amakode.log",
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

def reportJob(job):
    """ Report to amarok that the job is done """
    if (job.errormsg == None):
        log.debug("Job " + job.inurl + " completed successfully")
        log.debug("dcop amarok mediabrowser transcodingFinished " + job.inurl + " " + job.outurl)
    else:
        log.debug("Job " + job.inurl + " failed - " + job.errormsg)
        log.debug("dcop amarok mediabrowser transcodingFinished " + job.inurl + "\"\"")
    
if __name__ == "__main__":  
    initLog()
    signal.signal(signal.SIGINT, onStop)
    signal.signal(signal.SIGHUP, onStop)
    signal.signal(signal.SIGTERM, onStop)
    if 1:
        # Run normal application
        app = amaKode(sys.argv)
    else:
        # Quick test case
        q = QueueMgr(reportJob)
        j = TranscodeJob("file:///tmp/test.mp3", "ogg")
        q.add(j)
        j2 = TranscodeJob("file:///tmp/test2.mp3", "m4a")
        q.add(j2)
        while not q.isidle():
            q.poll()
            res = select.select([], [], [], 1)
        
        log.debug("jobs all done")
