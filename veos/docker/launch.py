#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import time

import vrnetlab


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class VEOS_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = ""
        for e in os.listdir("/"):
            if re.search(".vmdk$", e):
                disk_image = "/" + e
        if disk_image == "":
            logging.getLogger().info("Disk image was not found")
            exit(1)
        super(VEOS_vm, self).__init__(
            username, password, disk_image=disk_image, ram=2048
        )
        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 20
        self.qemu_args.extend(["-cpu", "host,level=9"])
        self.qemu_args.extend(["-smp", "2,sockets=1,cores=1"])

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.logger.info("To many spins with no result, restarting")
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.tn.expect([b"login:"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.debug("matched login prompt")
                self.logger.debug("trying to log in with 'admin'")
                self.wait_write("admin", wait=None)

                # run main config!
                self.bootstrap_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def bootstrap_config(self):
        """Do the actual bootstrap config"""
        self.logger.info("applying bootstrap configuration")
        self.wait_write("", None)
        self.wait_write("enable", ">")
        self.wait_write("configure")
        self.wait_write(
            "username %s secret 0 %s role network-admin"
            % (self.username, self.password)
        )

        # configure mgmt interface
        self.wait_write("interface Management 1")
        self.wait_write("ip address 10.0.0.15/24")
        self.wait_write("exit")
        self.wait_write("management api http-commands")
        self.wait_write("protocol unix-socket")
        self.wait_write("no shutdown")
        self.wait_write("exit")

        # gnmic config
        self.wait_write("management api gnmi")
        self.wait_write("transport grpc default")
        self.wait_write("no shutdown")
        self.wait_write("exit")

        # netconf config
        self.wait_write("management api netconf")
        self.wait_write("transport ssh default")
        self.wait_write("exit")

        self.wait_write(f"hostname {self.hostname}")

        self.wait_write("exit")
        self.wait_write("copy running-config startup-config")

    def gen_mgmt(self):
        """
        Augment base gen_mgmt function to add gnmi and socat forwarding
        """
        res = []

        # vEOS-lab requires its Ma1 interface to be the first in the bus, so let's hardcode it (addr 0x2)
        res.append("-device")
        res.append(
            self.nic_type + f",netdev=p00,mac={vrnetlab.gen_mac(0)},bus=pci.1,addr=0x2"
        )

        res.append("-netdev")
        res.append(
            "user,id=p00,net=10.0.0.0/24,tftp=/tftpboot,hostfwd=tcp::2022-10.0.0.15:22,hostfwd=udp::2161-10.0.0.15:161,hostfwd=tcp::2830-10.0.0.15:830,hostfwd=tcp::2080-10.0.0.15:80,hostfwd=tcp::2443-10.0.0.15:443,hostfwd=tcp::16030-10.0.0.15:6030"
        )
        vrnetlab.run_command(
            ["socat", "TCP-LISTEN:6030,fork", "TCP:127.0.0.1:16030"],
            background=True,
        )

        return res


class VEOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(VEOS, self).__init__(username, password)
        self.vms = [VEOS_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-xrv9k", help="Router hostname")
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    logger.debug(f"Environment variables: {os.environ}")
    vrnetlab.boot_delay()

    vr = VEOS(args.hostname, args.username, args.password, args.connection_mode)
    vr.start()
