#!/usr/bin/env python
import sys, os
sys.path.append( os.path.join( os.path.dirname(__file__), '..', 'lib' ) )
sys.path.append( os.path.join( os.path.dirname(__file__), '..') )
import init
import config
import misc
from dashd import DashDaemon
from models import Superblock, Proposal, GovernanceObject, Watchdog
from models import VoteSignals, VoteOutcomes, Transient
import socket
from misc import printdbg
import time
from bitcoinrpc.authproxy import JSONRPCException


# sync dashd gobject list with our local relational DB backend
def perform_dashd_object_sync(dashd):
    GovernanceObject.sync(dashd)

# delete old watchdog objects, create new when necessary
def watchdog_check(dashd):
    printdbg("in watchdog_check")
    # delete expired watchdogs
    for wd in Watchdog.expired(dashd):
        printdbg("\tFound expired watchdog [%s], voting to delete" % wd.object_hash)
        wd.vote(dashd, VoteSignals.delete, VoteOutcomes.yes)

    # now, get all the active ones...
    active_wd = Watchdog.active(dashd)
    active_count = active_wd.count()

    # none exist, submit a new one to the network
    if 0 == active_count:
        # create/submit one
        printdbg("\tNo watchdogs exist... submitting new one.")
        wd = Watchdog(created_at = int(time.time()))
        wd.submit(dashd)

    else:
        wd_list = sorted(active_wd, key=lambda wd: wd.object_hash)

        # highest hash wins
        winner = wd_list.pop()
        printdbg("\tFound winning watchdog [%s], voting VALID" % winner.object_hash)
        winner.vote(dashd, VoteSignals.valid, VoteOutcomes.yes)

        # if remaining Watchdogs exist in the list, vote delete
        for wd in wd_list:
            printdbg("\tFound losing watchdog [%s], voting DELETE" % wd.object_hash)
            wd.vote(dashd, VoteSignals.delete, VoteOutcomes.yes)

    printdbg("leaving watchdog_check")

def attempt_superblock_creation(dashd):
    import dashlib

    if not dashd.is_masternode():
        print("We are not a Masternode... can't submit superblocks!")
        return

    # query votes for this specific ebh... if we have voted for this specific
    # ebh, then it's voted on. since we track votes this is all done using joins
    # against the votes table
    #
    # has this masternode voted on *any* superblocks at the given event_block_height?
    # have we voted FUNDING=YES for a superblock for this specific event_block_height?

    event_block_height = dashd.next_superblock_height()

    if Superblock.is_voted_funding(event_block_height):
        # printdbg("ALREADY VOTED! 'til next time!")

        # vote down any new SBs because we've already chosen a winner
        for sb in Superblock.at_height(event_block_height):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(dashd, VoteSignals.funding, VoteOutcomes.no)

        # now return, we're done
        return

    if not dashd.is_govobj_maturity_phase():
        printdbg("Not in maturity phase yet -- will not attempt Superblock")
        return

    proposals = Proposal.approved_and_ranked(dashd)
    sb = dashlib.create_superblock(dashd, proposals, event_block_height)
    if not sb:
        printdbg("No superblock created, sorry. Returning.")
        return

    # find the deterministic SB w/highest object_hash in the DB
    dbrec = Superblock.find_highest_deterministic(sb.hex_hash())
    if dbrec:
        dbrec.vote(dashd, VoteSignals.funding, VoteOutcomes.yes)

        # any other blocks which match the sb_hash are duplicates, delete them
        for sb in Superblock.select().where(Superblock.sb_hash == sb.hex_hash()):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(dashd, VoteSignals.delete, VoteOutcomes.yes)

        printdbg("VOTED FUNDING FOR SB! We're done here 'til next superblock cycle.")
        return
    else:
        printdbg("The correct superblock wasn't found on the network...")

    # if we are the elected masternode...
    if (dashd.we_are_the_winner()):
        printdbg("we are the winner! Submit SB to network")
        sb.submit(dashd)

def check_object_validity(dashd):
    # vote (in)valid objects
    for gov_class in [Proposal, Superblock]:
        for obj in gov_class.select():
            obj.vote_validity(dashd)

def is_dashd_port_open(dashd):
    # test socket open before beginning, display instructive message to MN
    # operators if it's not
    port_open = False
    try:
        info = dashd.rpc_command('getinfo')
        port_open = True
    except (socket.error, JSONRPCException) as e:
        print("%s" % e)

    return port_open

def main():
    dashd = DashDaemon.from_dash_conf(config.dash_conf)

    # check dashd connectivity
    if not is_dashd_port_open(dashd):
        print("Cannot connect to dashd. Please ensure dashd is running and the JSONRPC port is open to Sentinel.")
        sys.exit(2)

    # check dashd sync
    if not dashd.is_synced():
        print("dashd not synced with network! Awaiting full sync before running Sentinel.")
        sys.exit(2)

    # ========================================================================
    # general flow:
    # ========================================================================
    #
    # load "gobject list" rpc command data & create new objects in local MySQL DB
    perform_dashd_object_sync(dashd)

    # delete old watchdog objects, create a new if necessary
    watchdog_check(dashd)

    # auto vote network objects as valid/invalid
    check_object_validity(dashd)

    # create a Superblock if necessary
    attempt_superblock_creation(dashd)


if __name__ == '__main__':
    # ensure another instance of Sentinel is not currently running
    mutex_key = 'SENTINEL_RUNNING'
    # assume that all processes expire after 'timeout_seconds' seconds
    timeout_seconds = 90

    is_running = Transient.get(mutex_key)
    if is_running:
        printdbg("An instance of Sentinel is already running -- aborting.")
        sys.exit(2)
    else:
        Transient.set(mutex_key, misc.now(), timeout_seconds)

    # locked to this instance -- perform main logic here
    main()

    Transient.delete(mutex_key)
