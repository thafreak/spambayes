#!/usr/bin/env python

"""An IMAP filter.  An IMAP message box is scanned and all non-scored
messages are scored and (where necessary) filtered.

The original filter design owed much to isbg by Roger Binns
(http://www.rogerbinns.com/isbg).

Usage:
    imapfilter [options]

	note: option values with spaces in them must be enclosed
	      in double quotes

        options:
            -d  dbname  : pickled training database filename
            -D  dbname  : dbm training database filename
            -t          : train contents of spam folder and ham folder
            -c          : classify inbox
            -h          : help
            -v          : verbose mode
            -p          : security option to prompt for imap password,
                          rather than look in options["imap", "password"]
            -e y/n      : expunge/purge messages on exit (y) or not (n)
            -i debuglvl : a somewhat mysterious imaplib debugging level
            -l minutes  : period of time between filtering operations
            -b          : Launch a web browser showing the user interface.

Examples:

    Classify inbox, with dbm database
        imapfilter -c -D bayes.db
        
    Train Spam and Ham, then classify inbox, with dbm database
        imapfilter -t -c -D bayes.db

    Train Spam and Ham only, with pickled database
        imapfilter -t -d bayes.db

Warnings:
    o This is very alpha.  The filter is currently being developed and
      tested.  We do *not* recommend using it on a production system unless
      you are confident that you can get your mail back if you lose it.  On
      the other hand, we do recommend that you test it for us and let us
      know if anything does go wrong.  Once this appears in a release,
      rather than just cvs, you can feel a *little* <wink> more confident
      about using it.
    o By default, the filter does *not* delete, modify or move any of your
      mail.  Due to quirks in how imap works, new versions of your mail are
      modified and placed in new folders, but there originals are still
      available.  These are flagged with the /Deleted flag so that you know
      that they can be removed.  Your mailer may not show these messages
      by default, but there should be an option to do so.  *However*, if
      your mailer automatically purges/expunges (i.e. permanently deletes)
      mail flagged as such, *or* if you set the imap_expunge option to
      True, then this mail will be irretrievably lost.
    
To Do:
    o Find a better way to remove old msg from info database when saving
      modified messages
    o IMAPMessage and IMAPFolder currently carry out very simple checks
      of responses received from IMAP commands, but if the response is not
      "OK", then the filter terminates.  Handling of these errors could be
      much nicer.
    o IMAP over SSL would be nice. (isbg has an example of how to do this)
    o The flags should be copied along with the message (especially
      the /Seen flag, but all of them, really).
    o Should the imap options server and port be combined?  i.e. instead
      of having server=imap.example.com and port=143, have
      server=imap.example.com:143?  This would be more consistent with
      pop3proxy and would get rid of one option at no real cost.
    o Suggestions?
"""

# This module is part of the spambayes project, which is Copyright 2002-3
# The Python Software Foundation and is covered by the Python Software
# Foundation license.

__author__ = "Tony Meyer <ta-meyer@ihug.co.nz>, Tim Stone"
__credits__ = "All the Spambayes folk."

from __future__ import generators

try:
    True, False
except NameError:
    # Maintain compatibility with Python 2.2
    True, False = 1, 0

import socket
import imaplib
import os
import re
import time
import sys
import getopt
from getpass import getpass
import email.Parser
from email.Utils import parsedate

from spambayes.Options import options
from spambayes import tokenizer, storage, message, Dibbler
from spambayes.UserInterface import UserInterfaceServer
from spambayes.ImapUI import IMAPUserInterface

# global IMAPlib object
global imap
imap = None

# global rfc822 fetch command
rfc822_command = "(RFC822.PEEK)"

class IMAPSession(imaplib.IMAP4):
    '''A class extending the IMAP4 class, with a few optimizations'''
    
    def __init__(self, server, port, debug):
        imaplib.Debug = debug  # this is a global in the imaplib module
        imaplib.IMAP4.__init__(self, server, port)
        # For efficiency, we remember which folder we are currently
        # in, and only send a select command to the IMAP server if
        # we want to *change* folders.  This function is used by
        # both IMAPMessage and IMAPFolder.
        self.current_folder = None
        self.current_folder_readonly = None

    def login(self, uid, pw):
        try:
            imaplib.IMAP4.login(self, uid, pw)  # superclass login
        except imaplib.IMAP4.error, e:
            if str(e) == "permission denied":
                print "There was an error logging in to the IMAP server."
                print "The userid and/or password may be incorrect."
                sys.exit()
            else:
                raise
    
    def logout(self, expunge):
        # sign off
        if expunge:
            self.expunge()
        imaplib.IMAP4.logout(self)  # superclass logout
        
    def SelectFolder(self, folder, readOnly=True, force=False):
        '''A method to point ensuing imap operations at a target folder'''
        
        if self.current_folder != folder or \
           self.current_folder_readonly != readOnly or force:
            # Occasionally, we need to force a command, because we
            # are interested in the response.  Things would be much
            # nicer if we cached this information somewhere.
            response = self.select(folder, readOnly)
            if response[0] != "OK":
                print "Invalid response to %s:\n%s" % (command, response)
                sys.exit(-1)
            self.current_folder = folder
            self.current_folder_readonly = readOnly
            return response

class IMAPMessage(message.SBHeaderMessage):
    def __init__(self, folder, id):
        message.Message.__init__(self)

        self.id = id
        self.folder = folder
        self.previous_folder = None

    def _check(self, response, command):
        if response[0] != "OK":
            print "Invalid response to %s:\n%s" % (command, response)
            sys.exit(-1)

    def extractTime(self):
        # When we create a new copy of a message, we need to specify
        # a timestamp for the message.  If the message has a date header
        # we use that.  Otherwise, we use the current time.
        message_date = self["Date"]
        if message_date is not None:
            return imaplib.Time2Internaldate(\
                       time.mktime(parsedate(message_date)))
        else:
            return imaplib.Time2Internaldate(time.time())

    def MoveTo(self, dest):
        # This move operation just changes where we think we are,
        # and we do an actual move on save (to avoid doing
        # this more than once)
        if self.previous_folder is None:
            self.previous_folder = self.folder
            self.folder = dest

    def Save(self):
        # we can't actually update the message with IMAP
        # so what we do is create a new message and delete the old one
        # we need to copy the flags as well
        response = imap.uid("FETCH", self.id, "(FLAGS INTERNALDATE)")
        self._check(response, 'fetch (flags internaldate)')
        response_pattern = r"[\d]+ \(UID [\w]+ FLAGS (\([\\\w]+\)) "
        response_pattern += r'INTERNALDATE ["]?([\w\-: ]+)["]?\)'
        mo = re.match(response_pattern, response[1][0])
        if mo is None:
            msg_time = self.extractTime()
            flags = None
        else:
            flags = mo.group(1)
            msg_time = mo.group(2)

        # See searching for new uid comments below
        old_id = self.id
        self["X-Spambayes-IMAP-OldID"] = old_id
                    
        response = imap.append(self.folder.name, None,
                               msg_time, self.as_string())
        self._check(response, 'append')

        if self.previous_folder is None:
            imap.SelectFolder(self.folder.name, False)
        else:
            imap.SelectFolder(self.previous_folder.name, False)
            self.previous_folder = None
        response = imap.uid("STORE", old_id, "+FLAGS.SILENT", "(\\Deleted)")
        self._check(response, 'store')

        # We need to update the uid, as it will have changed
        # Searching for the new message is full of problems.  Searching for
        # the text sends far too much data through the connection, and
        # doesn't work reliably anyway.  We instead search for a special
        # header that we add for this explicit purpose.
        imap.SelectFolder(self.folder.name, False)
        response = imap.uid("SEARCH", "HEADER", "X-Spambayes-IMAP-OldID",
                            old_id)
        self._check(response, 'search')
        new_id = response[1][0]

        # now that we know the new id, we need to correct the flags
        if flags != None:
            response = imap.uid("STORE", new_id, "+FLAGS.SILENT", flags)
            self._check(response, "store flags")

        #XXX This code to delete the old message id from the message
        #XXX info db and manipulate the message id, is a *serious* hack.
        #XXX There's gotta be a better way to do this.
        message.msginfoDB._delState(self)
        self.id = new_id
        self.modified()


class IMAPFolder(object):
    def __init__(self, folder_name, readOnly=True):
        self.name = folder_name

    def __cmp__(self, obj):
        '''Two folders are equal if their names are equal'''
        return cmp(self.name, obj.name)

    def _check(self, response, command):
        if response[0] != "OK":
            print "Invalid response to %s:\n%s" % (command, response)
            sys.exit(-1)

    def __iter__(self):
        '''IMAPFolder is iterable'''
        for key in self.keys():
            try:
                yield self[key]
            except KeyError:
                pass

    def recent_keys(self):
        '''Returns uids for all the messages in the folder that
        are flagged as recent, but not flagged as deleted.'''
        imap.SelectFolder(self.name, True)
        response = imap.uid("SEARCH", "(RECENT UNDELETED)")
        self._check(response, "SEARCH (RECENT UNDELETED)")
        return response[1][0].split(' ')

    def keys(self):
        '''Returns uids for all the messages in the folder'''
        # request message range
        response = imap.SelectFolder(self.name, True, True)
        total_messages = response[1][0]
        if total_messages == '0':
            return []
        response = imap.fetch("1:" + total_messages, "(UID FLAGS)")
        r = re.compile(r"[0-9]+ \(UID ([0-9]+) FLAGS \(([\\\w]*)\)\)")
        uids = []
        for i in response[1]:
            mo = r.match(i)
            if mo is not None:
                # We are not interested in messages marked as deleted
                if mo.group(2).lower() != "\\deleted":
                    uids.append(mo.group(1))
        return uids

    def __getitem__(self, key):
        '''Return message matching the given uid'''
        global rfc822_command
        imap.SelectFolder(self.name, True)
        # We really want to use RFC822.PEEK here, as that doesn't effect
        # the status of the message.  Unfortunately, it appears that not
        # all IMAP servers support this, even though it is in RFC1730
        response = imap.uid("FETCH", key, rfc822_command)
        if response[0] != "OK":
            rfc822_command = "(RFC822)"
            response = imap.uid("FETCH", key, rfc822_command)
        self._check(response, "uid fetch")
        messageText = response[1][0][1]
        # we return an instance of *our* message class, not the
        # raw rfc822 message

        msg = IMAPMessage(self, key)
        msg.setPayload(messageText)
        
        return msg
   
    def Train(self, classifier, isSpam):
        '''Train folder as spam/ham'''
        num_trained = 0
        for msg in self:
            if msg.GetTrained() == isSpam:
                classifier.unlearn(msg.asTokens(), not isSpam)
                # Once the message has been untrained, it's training memory
                # should reflect that on the off chance that for some reason
                # the training breaks, which happens on occasion (the
                # tokenizer is not yet perfect)
                msg.RememberTrained(None)

            if msg.GetTrained() is None:
                classifier.learn(msg.asTokens(), isSpam)
                num_trained += 1
                msg.RememberTrained(isSpam)

        return num_trained                

    def Filter(self, classifier, spamfolder, unsurefolder):
        for msg in self:
            if msg.GetClassification() is None:
                (prob, clues) = classifier.spamprob(msg.asTokens(), evidence=True)
                # add headers and remember classification
                msg.addSBHeaders(prob, clues)

                cls = msg.GetClassification()
                if cls == options["Hammie", "header_ham_string"]:
                    # we leave ham alone
                    pass
                elif cls == options["Hammie", "header_spam_string"]:
                    msg.MoveTo(spamfolder)
                else:
                    msg.MoveTo(unsurefolder)

                msg.Save()

            
class IMAPFilter(object):
    def __init__(self, classifier):
        self.spam_folder = IMAPFolder(options["imap", "spam_folder"])
        self.unsure_folder = IMAPFolder(options["imap", "unsure_folder"])
        self.classifier = classifier
        
    def Train(self):
        if options["globals", "verbose"]:
            t = time.time()

        if options["imap", "ham_train_folders"] != "":
            ham_training_folders = \
                                 options["imap", "ham_train_folders"].split()
            for fol in ham_training_folders:
                folder = IMAPFolder(fol)
                num_ham_trained = folder.Train(self.classifier, False)

        if options["imap", "spam_train_folders"] != "":
            spam_training_folders = \
                                  options["imap", "spam_train_folders"].split()
            for fol in spam_training_folders:
                folder = IMAPFolder(fol)
                num_spam_trained = folder.Train(self.classifier, True)

        if num_ham_trained or num_spam_trained:
            self.classifier.store()
        
        if options["globals", "verbose"]:
            print "Training took %s seconds, %s messages were trained" \
                  % (time.time() - t, num_ham_trained + num_spam_trained)

    def Filter(self):
        if options["globals", "verbose"]:
            t = time.time()
            
        for filter_folder in options["imap", "filter_folders"].split():
            folder = IMAPFolder(filter_folder, False)
            folder.Filter(self.classifier, self.spam_folder,
                          self.unsure_folder)
 
        if options["globals", "verbose"]:
            print "Filtering took", time.time() - t, "seconds."

 
def run():
    global imap
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hbtcvpl:e:i:d:D:')
    except getopt.error, msg:
        print >>sys.stderr, str(msg) + '\n\n' + __doc__
        sys.exit()

    bdbname = options["pop3proxy", "persistent_storage_file"]
    useDBM = options["pop3proxy", "persistent_use_database"]
    doTrain = False
    doClassify = False
    doExpunge = options["imap", "expunge"]
    imapDebug = 0
    sleepTime = 0
    promptForPass = False
    launchUI = False

    for opt, arg in opts:
        if opt == '-h':
            print >>sys.stderr, __doc__
            sys.exit()
        elif opt == '-d':
            useDBM = False
            bdbname = arg
        elif opt == '-D':
            useDBM = True
            bdbname = arg
        elif opt == "-b":
            launchUI = True
        elif opt == '-t':
            doTrain = True
        elif opt == '-p':
            promptForPass = True
        elif opt == '-c':
            doClassify = True
        elif opt == '-v':
            options["globals", "verbose"] = True
        elif opt == '-e':
            if arg == 'y':
                doExpunge = True
            else:
                doExpunge = False
        elif opt == '-i':
            imapDebug = int(arg)
        elif opt == '-l':
            sleepTime = int(arg) * 60

    if not (doClassify or doTrain or launchUI):
        print "-b, -c, or -t operands must be specified."
        print "Please use the -h operand for help."
        sys.exit()

    if (launchUI and (doClassify or doTrain)):
        print """
-b option is exclusive with -c and -t options.
The user interface will be launched, but no classification
or training will be performed."""

    server = options["imap", "server"]
    username = options["imap", "username"]
    if server == "" or username == "":
        print "You need to specify both a server and a username."
        sys.exit()

    if promptForPass:
        pwd = getpass()
    else:
        pwd = options["imap", "password"]

    bdbname = os.path.expanduser(bdbname)
    
    if options["globals", "verbose"]:
        print "Loading database %s..." % (bdbname),
    
    if useDBM:
        classifier = storage.DBDictClassifier(bdbname)
    else:
        classifier = storage.PickledClassifier(bdbname)

    if options["globals", "verbose"]:
        print "Done."            
                
    if server.find(':') > -1:
        server, port = server.split(':', 1)
        port = int(port)
    else:
        if options["imap", "use_ssl"]:
            port = 993
        else:
            port = 143
    imap = IMAPSession(server, port, imapDebug)

    imap_filter = IMAPFilter(classifier)

    # Web interface
    # XXX If someone is running *both* pop3proxy and imapfilter
    # XXX then there will be trouble since both interfaces are
    # XXX using the same port by default.
    if launchUI:
        imap.login(username, pwd)
        httpServer = UserInterfaceServer(options["html_ui", "port"])
        httpServer.register(IMAPUserInterface(classifier, imap))
        Dibbler.run(launchBrowser=launchUI)
    else:
        while True:
            imap.login(username, pwd)

            if doTrain:
                if options["globals", "verbose"]:
                    print "Training"
                imap_filter.Train()
            if doClassify:
                if options["globals", "verbose"]:
                    print "Classifying"
                imap_filter.Filter()

            imap.logout(doExpunge)
            
            if sleepTime:
                time.sleep(sleepTime)
            else:
                break

if __name__ == '__main__':
    run()
