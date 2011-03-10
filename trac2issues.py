#!/usr/bin/env python

##Script to convert Trac Tickets to GitHub Issues

import os, sys, time, math, simplejson
import urllib2, urllib, pprint
from datetime import datetime
from optparse import OptionParser

##Setup pp for debugging
pp = pprint.PrettyPrinter(indent=4)


parser = OptionParser()
parser.add_option('-t', '--trac', dest='trac', help='Path to the Trac project to export.')
parser.add_option('-a', '--account', dest='account', help='Name of the GitHub Account to import into. (If neither this nor --account is specified, user from your global git config will be used.)')
parser.add_option('-p', '--project', dest='project', help='Name of the GitHub Project to import into.')
parser.add_option('-x', '--closed', action="store_true", default=False, dest='closed', help='Include closed tickets.')
parser.add_option('-c', '--component', action="store_true", default=False, dest='component', help='Create a label for the Trac component.')
parser.add_option('-m', '--milestone', action="store_true", default=False, dest='milestone', help='Create a label for the Trac milestone.')
parser.add_option('-o', '--owner', action="store_true", default=False, dest='owner', help='Create a label for the Trac owner.')
parser.add_option('-r', '--reporter', action="store_true", default=False, dest='reporter', help='Add a comment naming the reporter.')
parser.add_option('-u', '--url', dest='url', help='The base URL for the trac install (will also link to the old ticket in a comment).')
parser.add_option('-g', '--org', dest='organization', help='Name of GitHub Organization (supercedes --account)')

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
        self.github = 'https://github.com/api/v2/json'
        try:
            self.db = self.env.get_db_cnx()
        except TracError, e:
            print_error(e.message)

        self.includeClosed = options.closed
        self.labelMilestone = options.milestone
        self.labelComponent = options.component
        self.labelOwner = options.owner
        self.labelReporter = options.reporter
        self.useURL = False
        self.organization = options.organization

        if options.url:
            self.useURL = "%s/ticket/" % options.url

        self.ghAuth()

        self.projectPath = '%s/%s' % (self.organization or self.account or self.login, self.project)

        self.checkProject()

        if self.useURL:
            print bold('Does this look like a valid trac url? [y/N]\n %s1234567' % self.useURL)
            go = sys.stdin.readline().strip().lower()

            if go[0:1] != 'y':
                print_error('Try Again..')

        ##We own this project..
        self._fetchTickets()


    def checkProject(self):
        url = "%s/repos/show/%s" % (self.github, self.projectPath)
        try:
            data = simplejson.load(urlopen(url))
        except urllib2.HTTPError, e:
            print_error("Could not connect to project at %s, does it exist? %s" % (url, e))
        if 'error' in data:
            print_error("%s: %s" % (self.projectPath, data['error'][0]['error']))

    def ghAuth(self):
        login = os.popen('git config --global github.user').read().strip()
        token = os.popen('git config --global github.token').read().strip()

        if not login:
            print_error('GitHub Login Not Found: need github.user in your global config')
        if not token:
            print_error('GitHub API Token Not Found: need github.token in your global config')

        self.login = login
        self.token = token

    def _fetchTickets(self):
        cursor = self.db.cursor()

        where = " where (status != 'closed') "
        if self.includeClosed:
            where = ""

        sql = "select id, summary, status, description, milestone, component, reporter, owner from ticket %s order by id" % where
        cursor.execute(sql)
        # iterate through resultset
        tickets = []
        for id, summary, status, description, milestone, component, reporter, owner in cursor:
            if milestone:
                milestone = milestone.replace(' ', '_')
            if component:
                component = component.replace(' ', '_')
            if owner:
                owner = owner.replace(' ', '_')
            if reporter:
                reporter = reporter.replace(' ', '_')

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
            'login': self.login,
            'token': self.token,
            'title': info['summary'],
            'body': info['description']
        }
        data = urlencode_utf8(out)

        url = "%s/issues/open/%s" % (self.github, self.projectPath)
        req = urllib2.Request(url, data)
        response = urlopen(req)
        ticket_data = simplejson.load(response)

        if 'number' in ticket_data['issue']:
            num = ticket_data['issue']['number']
            print bold('Issue #%s created.' % num)
        else:
            print_error('GitHub didn\'t return an issue number :(')

        def info_has_key(key):
            value = info.get(key)
            if value is not None and value.strip() not in ('(none)', ''):
                return value
            return False

        if self.labelMilestone and info_has_key('milestone'):
            self.createLabel(num, "%s" % info['milestone'])

        if self.labelComponent and info_has_key('component'):
            self.createLabel(num, "C_%s" % info['component'])

        if self.labelOwner and info_has_key('owner'):
            self.createLabel(num, "@%s" % info['owner'])

        if self.labelReporter and info_has_key('reporter'):
            self.createLabel(num, "@@%s" % info['reporter'])

        for i in info['history']:
            # We don't really want comments with nothing but an author, do we?
            if not i['comment']:
                continue
            if i['author']:
                comment = "Author: %s\n%s" % (i['author'], i['comment'])
            else:
                comment = i['comment']
            self.addComment(num, comment)

        if self.useURL:
            comment = "Ticket imported from Trac:\n %s%s" % (self.useURL, info['id'])
            self.addComment(num, comment)

        if info.get('status') == 'closed':
            self.closeIssue(num)


    def createLabel(self, num, name):
        name = name.replace('/', '-')
        name = urllib2.quote(name)
        print bold("\tAdding label %s to issue # %s" % (name, num))
        url = "%s/issues/label/add/%s/%s/%s" % (self.github, self.projectPath, name, num)
        out = {
            'login': self.login,
            'token': self.token
        }
        data = urlencode_utf8(out)
        req = urllib2.Request(url, data)
        response = urlopen(req)
        label_data = simplejson.load(response)

    def addComment(self, num, comment):
        comment = comment.strip()
        if not comment:
            print bold("\tSkipping empty comment on issue # %s" % num)
            return
        print bold("\tAdding comment to issue # %s" % num)
        url = "%s/issues/comment/%s/%s" % (self.github, self.projectPath, num)
        out = {
            'login': self.login,
            'token': self.token,
            'comment': comment
        }
        data = urlencode_utf8(out)
        req = urllib2.Request(url, data)
        response = urlopen(req)

    def closeIssue(self, num):
        print bold("\tClosing issue # %s" %  num)
        url = "%s/issues/close/%s/%s" % (self.github, self.projectPath, num)
        out = {
            'login': self.login,
            'token': self.token
        }
        data = urlencode_utf8(out)
        req = urllib2.Request(url, data)
        response = urlopen(req)
        close_data = simplejson.load(response)


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


