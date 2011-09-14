#!/usr/bin/env python
'''
Copyright (C) 2010, Digium, Inc.
Paul Belanger <pabelanger@digium.com>

This program is free software, distributed under the terms of
the GNU General Public License Version 2.
'''

import sys
import logging
import logging.config
import os
import datetime
import time
from twisted.internet import reactor
from starpy import manager, fastagi

from asterisk import Asterisk
from TestConfig import TestConfig
from TestConditions import TestConditionController, TestCondition
from ThreadTestCondition import ThreadPreTestCondition, ThreadPostTestCondition

logger = logging.getLogger(__name__)

class TestCase(object):
    """
    The base class object for python tests.  This class provides common functionality to all
    tests, including management of Asterisk instances, AMI, Twisted Reactor, and various
    other utilities.
    """

    def __init__(self):
        """
        Create a new instance of a TestCase.  Must be called by inheriting
        classes.
        """

        self.test_name = os.path.dirname(sys.argv[0])
        self.base = self.test_name.lstrip("tests/")
        self.ast = []
        self.ami = []
        self.fastagi = []
        self.reactor_timeout = 30
        self.passed = False
        self.defaultLogLevel = "WARN"
        self.defaultLogFileName = "logger.conf"
        self.timeoutId = None
        self.test_config = TestConfig(self.test_name)
        self.testStateController = None

        """ Set up logging """
        logConfigFile = os.path.join(os.getcwd(), "%s" % (self.defaultLogFileName))
        if os.path.exists(logConfigFile):
            logging.config.fileConfig(logConfigFile, None, False)
        else:
            print "WARNING: no logging.conf file found; using default configuration"
            logging.basicConfig(level=self.defaultLogLevel)

        self.testConditionController = TestConditionController(self.test_config, self.ast, self.stop_reactor)
        self.__setup_conditions()

        logger.info("Executing " + self.test_name)
        reactor.callWhenRunning(self.run)

    def __setup_conditions(self):
        """
        Register pre and post-test conditions.  Note that we have to first register condition checks
        without related conditions, so that those that have dependencies can find them
        """
        self.conditions = self.test_config.get_conditions()
        for c in self.conditions:
            """ c is a 3-tuple of object, pre-post type, and related name """
            if (c[2] == ""):
                if (c[1] == "PRE"):
                    self.testConditionController.register_pre_test_condition(c[0])
                elif (c[1] == "POST"):
                    self.testConditionController.register_post_test_condition(c[0])
                else:
                    logger.warning("Unknown condition type [%s]" % c[1])
        for c in self.conditions:
            if (c[2] != ""):
                if (c[1] == "POST"):
                    self.testConditionController.register_post_test_condition(c[0], c[2])
                else:
                    logger.warning("Unsupported type [%s] with related condition %s" % (c[1], c[2]))
        self.testConditionController.register_observer(self.handle_condition_failure, 'Failed')

    def create_asterisk(self, count=1):
        """
        Create n instances of Asterisk

        Keyword arguments:
        count -- the number of Asterisk instances to create.  Each Asterisk instance
        will be hosted on 127.0.0.x, where x is the 1-based index of the instance
        created

        """
        for c in range(count):
            logger.info("Creating Asterisk instance %d" % (c + 1))
            host = "127.0.0.%d" % (c + 1)
            self.ast.append(Asterisk(base=self.base, host=host))
            # Copy shared config files
            self.ast[c].install_configs("%s/configs" %
                    (self.test_name))
            # Copy test specific config files
            self.ast[c].install_configs("%s/configs/ast%d" %
                    (self.test_name, c + 1))

    def create_ami_factory(self, count=1, username="user", secret="mysecret", port=5038):
        """
        Create n instances of AMI.  Each AMI instance will attempt to connect to
        a previously created instance of Asterisk.  When a connection is complete,
        the ami_connect method will be called.

        Keyword arguments:
        count -- The number of instances of AMI to create
        username -- The username to login with
        secret -- The password to login with
        port -- The port to connect over

        """

        for c in range(count):
            host = "127.0.0.%d" % (c + 1)
            self.ami.append(None)
            logger.info("Creating AMIFactory %d" % (c + 1))
            self.ami_factory = manager.AMIFactory(username, secret, c)
            self.ami_factory.login(host).addCallbacks(self.__ami_connect,
                self.ami_login_error)

    def create_fastagi_factory(self, count=1):
        """
        Create n instances of AGI.  Each AGI instance will attempt to connect to
        a previously created instance of Asterisk.  When a connection is complete,
        the fastagi_connect method will be called.

        Keyword arguments:
        count -- The number of instances of AGI to create

        """

        for c in range(count):
            host = "127.0.0.%d" % (c + 1)
            self.fastagi.append(None)
            logger.info("Creating FastAGI Factory %d" % (c + 1))
            self.fastagi_factory = fastagi.FastAGIFactory(self.fastagi_connect)
            reactor.listenTCP(4573, self.fastagi_factory,
                    self.reactor_timeout, host)

    def start_asterisk(self):
        """
        Start the instances of Asterisk that were previously created.  See
        create_asterisk.  Note that this should be called before the reactor is
        told to run.
        """
        for index, item in enumerate(self.ast):
            logger.info("Starting Asterisk instance %d" % (index + 1))
            self.ast[index].start()
        self.testConditionController.evaluate_pre_checks()

    def stop_asterisk(self):
        """
        Stop the instances of Asterisk that were previously started.  See
        start_asterisk.  Note that this should be called after the reactor has
        returned from its run.
        """
        for amiInstance in self.ami:
            try:
                logger.debug("Logging off of AMI instance %d" % amiInstance.id)
                amiInstance.logoff()
            except:
                logger.warning("Exception occurred while logging off of AMI instance %d" % amiInstance.id)

        """ Pause for one second to allow dialplan to catch up with AMI logoffs """
        time.sleep(1)
        self.testConditionController.evaluate_post_checks()
        for index, item in enumerate(self.ast):
            logger.info("Stopping Asterisk instance %d" % (index + 1))
            self.ast[index].stop()

    def stop_reactor(self):
        """
        Stop the reactor and cancel the test.
        """
        logger.info("Stopping Reactor")
        if reactor.running:
            reactor.stop()

    def __reactor_timeout(self):
        '''
        A wrapper function for stop_reactor(), so we know when a reactor timeout
        has occurred.
        '''
        logger.warning("Reactor timeout: '%s' seconds" % self.reactor_timeout)
        self.stop_reactor()

    def run(self):
        """
        Base implementation of the test execution method, run.  Derived classes
        should override this and start their Asterisk dependent logic from this method.
        Derived classes must call this implementation, as this method provides a fail
        out mechanism in case the test hangs.
        """
        if (self.reactor_timeout > 0):
            self.timeoutId = reactor.callLater(self.reactor_timeout, self.__reactor_timeout)

    def ami_login_error(self, ami):
        """
        Handler for login errors into AMI.  This will stop the test.

        Keyword arguments:
        ami -- The instance of AMI that raised the login error

        """
        logger.error("Error logging into AMI")
        self.stop_reactor()

    def ami_connect(self, ami):
        """
        Hook method used after create_ami_factory() successfully logs into
        the Asterisk AMI.
        """
        pass

    def __ami_connect(self, ami):
        logger.info("AMI Connect instance %s" % (ami.id + 1))
        self.ami[ami.id] = ami
        self.ami_connect(ami)

    def handleOriginateFailure(self, reason):
        """
        Convenience callback handler for twisted deferred errors for an AMI originate call.  Derived
        classes can choose to add this handler to originate calls in order to handle them safely when
        they fail.  This will stop the test if called.

        Keyword arguments:
        reason -- The reason the originate failed
        """
        logger.error("Error sending originate:")
        logger.error(reason.getTraceback())
        self.stop_reactor()
        return reason

    def reset_timeout(self):
        """
        Resets the reactor timeout
        """
        if (self.timeoutId != None):
            originalTime = datetime.datetime.fromtimestamp(self.timeoutId.getTime())
            self.timeoutId.reset(self.reactor_timeout)
            newTime = datetime.datetime.fromtimestamp(self.timeoutId.getTime())
            logger.info("Reactor timeout originally scheduled for %s, rescheduled for %s" % (str(originalTime), str(newTime)))

    def handle_condition_failure(self, test_condition):
        """
        Callback handler for condition failures
        """
        if test_condition.pass_expected:
            logger.error("Test Condition %s failed; setting test passed status to False" % test_condition.getName())
            self.passed = False
        else:
            logger.info("Test Condition %s failed but expected failure was set; test status not modified" % test_condition.getName())
