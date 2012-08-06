#!/usr/bin/env python

##Script to convert Trac Tickets to GitHub Issues

import re, os, sys, time, math, simplejson
import string, shutil, urllib2, urllib, pprint, datetime, base64, json, getpass
from datetime import datetime
from optparse import OptionParser
from time import sleep

##Setup pp for debugging
pp = pprint.PrettyPrinter(indent=4)


parser = OptionParser()
parser.add_option('-t', '--trac', dest='trac', help='Path to the Trac project to export.')
parser.add_option('-a', '--account', dest='account', help='Name of the GitHub Account to import into. (If not specified, user from your global git config will be used.)')
parser.add_option('-p', '--project', dest='project', help='Name of the GitHub Project to import into.')
parser.add_option('-x', '--closed', action="store_true", default=False, dest='closed', help='Include closed tickets.')
parser.add_option('-y', '--type', action="store_true", default=False, dest='type', help='Create a label for the Trac ticket type.')
parser.add_option('-c', '--component', action="store_true", default=False, dest='component', help='Create a label for the Trac component.')
parser.add_option('-m', '--milestone', action="store_true", default=False, dest='milestone', help='Create a label for the Trac milestone.')
parser.add_option('-r', '--reporter', action="store_true", default=False, dest='reporter', help='Create a label for the Trac reporter.')
parser.add_option('-o', '--owner', action="store_true", default=False, dest='owner', help='Create a label for the Trac owner.')
parser.add_option('-u', '--url', dest='url', help='Base URL for the Trac install (if specified, will create a link to the old ticket in a comment).')
parser.add_option('-g', '--org', dest='organization', help='Name of GitHub Organization (supercedes --account)')
parser.add_option('-s', '--start', dest='start', help='The trac ticket to start importing at.')
parser.add_option('--authors', dest='authors_file', default='authors.txt',
                  help='File to load assignee login names from. Each line is space-separated like: trac-login github-login')
(options, args) = parser.parse_args(sys.argv[1:])


# Monkeypatch urllib2 to not treat HTTP 20x as an error.
# Is there a better way to do this?
def _non_stupid_http_response(self, request, response):
    code, msg, hdrs = response.code, response.msg, response.info()
    if code < 200 or code > 206:
        response = self.parent.error(
            'http', request, response, code, msg, hdrs)
    return response

urllib2.HTTPErrorProcessor.http_response = _non_stupid_http_response
urllib2.HTTPErrorProcessor.https_response = _non_stupid_http_response

GITHUB_MAX_PER_MINUTE=60
_last_ran_at = time.time()

def urlopen(*args, **kw):
    # As per http://develop.github.com/p/general.html they're limiting
    # to GITHUB_MAX_PER_MINUTE calls per minute.

    # Normally we wait ~1 second between calls to avoid hitting the
    # rate limit and having to pause a long time. And to be nice.
    # (By keeping track of when we actually last ran, we avoid sleeping
    # longer than needed.)
    global _last_ran_at
    when_to_run = _last_ran_at + (60.0 / GITHUB_MAX_PER_MINUTE)
    sleeptime = max(0, when_to_run - time.time())
    time.sleep(sleeptime)
    _last_ran_at = time.time()

    try:
        return urllib2.urlopen(*args, **kw)
    except urllib2.HTTPError, e:
        if e.code == 403:
            # Maybe we recently ran some other script that hit the rate limit?
            print bold('Permission denied, waiting a minute and trying again once...')
            time.sleep(61)
            return urllib2.urlopen(*args, **kw)
        else:
            raise

class ImportTickets:

    def __init__(self, trac=options.trac, account=options.account, project=options.project):
        self.env = open_environment(trac)
        self.trac = trac
        self.account = account
        self.project = project
        self.now = datetime.now(utc)
        #Convert the timestamp from a float to an int to drop the .0
        self.stamp = int(math.floor(time.time()))
        self.github = 'https://api.github.com'
        try:
            self.db = self.env.get_db_cnx()
        except TracError, e:
            print_error(e.message)

        self.includeClosed = options.closed
        self.labelType = options.type
        self.labelMilestone = options.milestone
        self.labelComponent = options.component
        self.labelOwner = options.owner
        self.labelReporter = options.reporter
        self.start = options.start
        self.useURL = False
        self.organization = options.organization
        self.reqCount = 0

        if options.url:
            self.useURL = "%s/ticket/" % options.url

        self.ghAuth()

        self.projectPath = '%s/%s' % (self.organization or self.account or self.login, self.project)

        self.checkProject()
        self.milestones = self.loadMilestones()
        self.contributors = self.loadContributors()
        self.labels = self.loadLabels()

        if self.useURL:
            print bold('Does this look like a valid trac url? [y/N]\n %s1234567' % self.useURL)
            go = sys.stdin.readline().strip().lower()

            if go[0:1] != 'y':
                print_error('Try Again..')


        ##We own this project..
        self._fetchTickets()

    def checkProject(self):
        url = "%s/repos/%s" % (self.github, self.projectPath)
        try:
            data = simplejson.load(urlopen(url))
        except urllib2.HTTPError, e:
            print_error("Could not connect to project at %s, does it exist? %s" % (url, e))
        if 'error' in data:
            print_error("%s: %s" % (self.projectPath, data['error'][0]['error']))

    def ghAuth(self):
        login = os.popen('git config --global github.user').read().strip()

        if not login:
            print_error('GitHub Login Not Found: need github.user in your global config')

        self.login = login
        print "Gitub password for %s" % login
        self.password = getpass.getpass()

    def _fetchTickets(self):
        cursor = self.db.cursor()

        where = " where (status != 'closed') "
        if self.includeClosed:
            where = ""

        if self.start:
            if where:
                where += " and id >= %s" % self.start
            else:
                where = ' where id >= %s' % self.start

        sql = "select id, summary, status, description, milestone, component, reporter, owner, type from ticket %s order by id" % where
        cursor.execute(sql)
        # iterate through resultset
        tickets = []
        for id, summary, status, description, milestone, component, reporter, owner, type in cursor:
            if milestone:
                milestone = milestone.replace(' ', '_')
            if component:
                component = component.replace(' ', '_')
            if owner:
                owner = owner.replace(' ', '_')
            if reporter:
                reporter = reporter.replace(' ', '_')
            if type:
                type = type.replace(' ', '_')

            ticket = {
                'id': id,
                'summary': summary,
                'status': status,
                'description': description,
                'milestone': milestone,
                'component': component,
                'reporter': reporter,
                'owner': owner,
                'history': [],
                'status': status,
                'type': type,
            }
            cursor2 = self.db.cursor()
            sql = 'select author, time, newvalue from ticket_change where (ticket = %s) and (field = "comment")' % id
            cursor2.execute(sql)
            for author, time, newvalue in cursor2:
                change = {
                    'author': author,
                    'time': time,
                    'comment': newvalue
                }
                ticket['history'].append(change)

            tickets.append(ticket)

        print bold('About to import (%s) tickets from Trac to %s.\n%s? [y/N]' % (len(tickets), self.projectPath, red('Are you sure you wish to continue')))
        go = sys.stdin.readline().strip().lower()

        if go[0:1] != 'y':
            print_error('Import Aborted..')


        #pp.pprint(tickets)
        for data in tickets:
            self.createIssue(data)


    def createIssue(self, info):
        print bold('Creating issue from ticket %s' % info['id'])
        out = {
            'title': info['summary'].encode('utf-8'),
            'body': info['description'].encode('utf-8'),
            'labels': [],
        }

        def info_has_key(key):
            value = info.get(key)
            if value is not None and value.strip() not in ('(none)', '', 'Unassigned'):
                return value
            return False

        if self.labelMilestone and info_has_key('milestone'):
            out['milestone'] = self.getMilestone(info['milestone'])

        if self.labelType and info_has_key('type'):
            out['labels'].append(info['type'])

        if self.labelComponent and info_has_key('component'):
            out['labels'].append(info['component'])

        if self.labelOwner and info_has_key('owner'):
            if info['owner'] in self.contributors:
                out['assignee'] = self.contributors[info['owner']]

        if self.labelReporter and info_has_key('reporter'):
            # Unfortunately github api v3 still has no way to specify the
            # creator of an issue.
            out['labels'].append("@@%s" % info['reporter'])

        for label in out['labels']:
            # Labels must exist before being assigned to tickets.
            self.createLabel(label)

        url = "%s/repos/%s/issues" % (self.github, self.projectPath)
        response = self.makeRequest(url, out)
        ticket_data = simplejson.load(response)

        if 'number' in ticket_data:
            num = ticket_data['number']
            print bold('Issue #%s created.' % num)
        else:
            print_error('GitHub didn\'t return an issue number :(')

        for i in info['history']:
            if i['comment']: 
                if i['author']:
                    comment = "Author: %s\n%s" % (i['author'].encode('utf-8','replace'), i['comment'].encode('utf-8','replace'))
                else:
                    comment = i['comment'].encode('utf-8','replace')
                    
                self.addComment(num, comment)

        if self.useURL:
            comment = "Ticket imported from Trac:\n %s%s" % (self.useURL, info['id'])
            self.addComment(num, comment)

        if info.get('status') == 'closed':
            self.closeTicket(num)

    def createLabel(self, name):
        # Can't add a label to a ticket unless it exists, humph.
        if name in self.labels:
            return
        print bold("\tAdding label %s" % (name,))
        url = "%s/repos/%s/labels" % (self.github, self.projectPath)
        out = {'name': name,
               'color': "FFFFFF"}

        self.makeRequest(url, out)
        self.labels.add(name)

    def getMilestone(self, name):
        if name in self.milestones:
            return self.milestones[name]
        url = "%s/repos/%s/milestones" % (self.github, self.projectPath)
        out = {
            'title': name
        }
        response = self.makeRequest(url, out)
        milestone_data = simplejson.load(response)
        num = milestone_data['number']
        self.milestones[name] = num
        return num

    def loadMilestones(self):
        milestones = {}
        self.loadMilestonesForStatus('open', milestones)
        self.loadMilestonesForStatus('closed', milestones)
        return milestones

    def loadMilestonesForStatus(self, param, milestones):
        url = "%s/repos/%s/milestones?state=%s" % (self.github, self.projectPath, param)
        response = self.makeRequest(url, None)
        milestones_data = simplejson.load(response)
        for milestone_data in milestones_data:
            print 'Found milestone %s' % milestone_data['title']
            milestones[milestone_data['title']] = milestone_data['number']

    def loadContributors(self):
        if (os.path.exists(self.authors_file)):
            with open(self.authors_file) as fd:
                collaborators = dict(line.strip().split(None, 1) for line in fd)
        else:
            collaborators = {}
        url = "%s/repos/%s/collaborators" % (self.github, self.projectPath)
        response = self.makeRequest(url, None)
        collaborators_data = simplejson.load(response)
        for collaborator_data in collaborators_data:
            login = collaborator_data['login']
            collaborators.setdefault(login, login)
        return collaborators

    def loadLabels(self):
        url = '%s/repos/%s/labels' % (self.github, self.projectPath)
        response = self.makeRequest(url, None)
        labels = [label['name'] for label in simplejson.load(response)]
        return set(labels)

    def addComment(self, num, comment):
        comment = comment.strip()
        if not comment:
            print bold("\tSkipping empty comment on issue # %s" % num)
            return
        print bold("\tAdding comment to issue # %s" % num)
        url = "%s/repos/%s/issues/%s/comments" % (self.github, self.projectPath, num)
        out = {
            'body': comment
        }
        response = self.makeRequest(url, out)

    def closeTicket(self, num):
        url = "%s/repos/%s/issues/%s" % (self.github, self.projectPath, num)
        out = {
            'state': 'closed'
        }
        response = self.makeRequest(url, out)

    def makeRequest(self, url, out):
        req = urllib2.Request(url) if out is None else urllib2.Request(url, json.dumps(out))

        base64string = base64.encodestring(
                        '%s:%s' % (self.login, self.password))[:-1]
        authheader =  "Basic %s" % base64string
        req.add_header("Authorization", authheader)
        print url
        print json.dumps(out)
        self.reqCount += 1
        if (self.reqCount % GITHUB_MAX_PER_MINUTE == 0):
            self.apiLimitExceeded()
        print "Request no: %s" % (self.reqCount)
        try:
            response = urllib2.urlopen(req)
        except urllib2.HTTPError, err:
            if err.code == 403:
               self.apiLimitExceeded()
               response = self.makeRequest(url, out) 
            elif err.code >= 400:
                sys.stderr.write(red("HTTP error!\n"))
                sys.stderr.write(err.read() + '\n')
                raise
            else:
                raise

        return response

    def apiLimitExceeded(self):
        self.reqCount = 0
        print "Sleeping for 60 seconds"
        sleep(60)

def urlencode_utf8(adict):
    """Ensure dict's values are all utf-8 before urlencoding it.
    """
    data = urllib.urlencode(dict([k, v.encode('utf-8')]
                                 for k, v in adict.items()))
    return data

##Format bold text
def bold(str):
    return "\033[1m%s\033[0m" % str

##Format red text (for errors)
def red(str):
    return "\033[31m%s\033[0m" % str

##Print and format an error, then exit the script
def print_error(str):
    print  bold(red(str))
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print "For usage: %s --help" % (sys.argv[0])
        print
    else:
        if not options.trac or not options.project:
            print_error("For usage: %s --help" % (sys.argv[0]))

        os.environ['PYTHON_EGG_CACHE'] = '/tmp/.egg-cache'
        os.environ['TRAC_ENV'] = options.trac
        from trac.core import TracError
        from trac.env import open_environment
        from trac.util.datefmt import utc
        ImportTickets()
