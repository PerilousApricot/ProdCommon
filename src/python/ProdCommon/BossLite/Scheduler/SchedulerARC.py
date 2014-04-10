#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Scheduler for the Nordugrid ARC middleware.
#
# Maintainers:
# Erik Edelmann <erik.edelmann@ndgf.fi>
# 

"""
_SchedulerARC_
"""


import os
import tempfile
#import socket
#import tempfile
from ProdCommon.BossLite.Scheduler.SchedulerInterface import SchedulerInterface
from ProdCommon.BossLite.Common.Exceptions import SchedulerError
from ProdCommon.BossLite.DbObjects.Job import Job
from ProdCommon.BossLite.DbObjects.Task import Task
import logging
import re
#import arclib as arc

#
# Mapping from ARC status codes to BossLite dito.
#
# Meaning ARC status codes StatusReason table below.
# BossLite status code docs:
# https://twiki.cern.ch/twiki/bin/view/CMS/BossLiteJob
#

Arc2Status = {
    "ACCEPTING": "SU",
    "ACCEPTED":  "SU",
    "PREPARING": "SW",
    "PREPARED":  "SW",
    "SUBMITTING":"SR",
    "INLRMS:Q":  "SS",
    "INLRMS:R":  "R",
    "INLRMS:S":  "R",
    "INLRMS:E":  "R",
    "INLRMS:O":  "R",
    "EXECUTED":  "R",
    "FINISHING": "R",
    "KILLING":   "K",
    "KILLED":    "K",
    "DELETED":   "A",
    "FAILED":    "DA",
    "FINISHED":  "SD",

    # In addition, let's define a few of our own
    "UNKNOWN":     "UN",
    "WTF?":        "UN"
}

Arc2StatusScheduler = {
    "ACCEPTING": "Submitted",
    "ACCEPTED":  "Submitted",
    "PREPARING": "Waiting",
    "PREPARED":  "Waiting",
    "SUBMITTING":"Ready",
    "INLRMS:Q":  "Scheduled",
    "INLRMS:R":  "Running",
    "INLRMS:S":  "Running",
    "INLRMS:E":  "Running",
    "INLRMS:O":  "Running",
    "EXECUTED":  "Running",
    "FINISHING": "Running",
    "KILLING":   "Killed/Cancelled",
    "KILLED":    "Killed/Cancelled",
    "DELETED":   "Aborted",
    "FAILED":    "Done (failed)",
    "FINISHED":  "Done (success)",

    # In addition, let's define a few of our own
    "UNKNOWN":     "Undefined/Unknown",
    "WTF?":        "Undefined/Unknown"
}

Arc2StatusReason = {
    "ACCEPTING": "Job has reaced the CE",
    "ACCEPTED":  "Job submitted but not yet processed",
    "PREPARING": "Input files are being transferred",
    "PREPARED":  "Transferring input files done",
    "SUBMITTING":"Interaction with the LRMS at the CE ongoing",
    "INLRMS:Q":  "In the queue of the LRMS at the CE",
    "INLRMS:R":  "Running",
    "INLRMS:S":  "Suspended",
    "INLRMS:E":  "About to finish in the LRMS",
    "INLRMS:O":  "Other LRMS state",
    "EXECUTED":  "Job is completed in the LRMS",
    "FINISHING": "Output files are being transferred",
    "KILLING":   "Job is being cancelled on user request",
    "KILLED":    "Job canceled on user request",
    "DELETED":   "Job removed due to expiration time",
    "FAILED":    "Job finished with an error.",
    "FINISHED":  "Job finished successfully.",

    "UNKNOWN":    "Job not known by ARC server (or info.sys. too slow!)",
    "WTF?":       "Job not recognized as a job by the ARC client!"
}


def ARCInfoSplitClusters(output):
    """
    Take the output of 'arcinfo -l', and split it into text
    chunks; one chunck per cluster.
    """

    cluster = []
    for line in output.split('\n'):
        if line.find("Computing service:") >= 0:
            if cluster:
                yield cluster
            cluster = [ line ]
        else:
            cluster.append(line)
    if cluster:
        yield cluster


def splitARCStatOutput(output):
     """
     Split a string of arcstat output into a list with one job per list
     item.

     The assumption is that the first line of a job has no indentation,
     and subsequent lines are indented by at least 1 space or start with "This job was only very recently submitted".
     """

     jobs = []
     s = ""
     for line in output.split('\n'):

          if len(line) == 0 or re.search("no jobs", line, flags=re.I) or line.split()[0] in ['DEBUG:', 'INFO:', 'VERBOSE:']:
               continue

          if line[0].isspace():
               s += '\n' + line
          elif re.search("this job was.* recently submitted", line, flags=re.I):
               s += ' ' + line
          else:
               if len(s) > 0:
                    jobs.append(s + '\n')
               s = line 
     if len(s) > 0:
          jobs.append(s)

     return jobs


def count_nonempty(list):
    """Count number of non-empty/non-false items"""
    n = 0
    for i in list:
        if i: n += 1
    return n


def get_arcsub_opts(requirements):
    assert type(requirements) == dict

    opt = ""
    if "clusters" in requirements:
        for c in requirements["clusters"]:
            opt += " -c " + c
    return opt


class SchedulerARC(SchedulerInterface):
    """
    basic class to handle ARC jobs
    """

    def __init__(self, **args):
        super(SchedulerARC, self).__init__(**args)
        self.vo = args.get("vo", "cms")
        self.accepted_CEs = []
        self.user_xrsl = args.get("user_xrsl", "")
        self.scheduler = "ARC"
        self.logging.debug("self.cert =" + self.cert)

        if self.cert:
            self.pre_arcCmd = "X509_USER_PROXY=" + self.cert + " "
        else:
            self.pre_arcCmd = ""


    def jobDescription(self, obj, requirements={}, config='', service = ''):
        """
        retrieve scheduler specific job description
        return it as a string
        """
        assert type(obj) == Task

        xrsl = "+\n"
        for job in obj.getJobs():
            xrsl += '(' +  self.decode(job, obj, requirements) + ')\n'
        return xrsl

        
    def decode(self, job, task, requirements={}):
        """
        prepare scheduler specific job description

        used by self.submit(), return xrsl code.
        """

        assert type(requirements) == dict

        xrsl = '&'
        xrsl += '(executable="%s")' % job['executable']

        # An argument-string may contain '\"':s and '\':s
        # that should be removed -- otherwise it will be split into
        # several arguments by the shell, which is WRONG!
        if job['arguments']:
            args = job['arguments'].replace('\\"', '').replace('\\', '')
            xrsl += '(arguments=%s)' % args

        xrsl += '(jobName="%s")' % job['name']
        xrsl += '(stdout="%s")' % job['standardOutput']
        xrsl += '(stderr="%s")' % job['standardError']
        if job['standardInput'] != '':
            xrsl += '(stdin="%s")' % job['standardInput']

        inputfiles = ""
        xrsl += '(inputFiles='
        for f in task['globalSandbox'].split(','):
            xrsl += '(%s %s)' % (f.split('/')[-1], f)
            inputfiles += " " + f.split('/')[-1]
        for f in job['inputFiles']:
            xrsl += '(%s %s)' % (f.split('/')[-1], f)
            inputfiles += " " + f.split('/')[-1]
        xrsl += ')'

        if task['outputDirectory'] and task['outputDirectory'].find('gsiftp://') >= 0:
            destUrl = lambda f: task['outputDirectory'] + "/" +  f
        else:
            destUrl = lambda f: '""'

        outputfiles = ""
        if len(job['outputFiles']) > 0:
            xrsl += '(outputFiles='
            for f in job['outputFiles']:
                xrsl += '(%s %s)' % (f, destUrl(f))
                outputfiles += " " + f
            xrsl += ')'

        xrsl += "(environment="
        xrsl += "(ARC_INPUTFILES \"%s\")(ARC_OUTPUTFILES \"%s\")" % (inputfiles, outputfiles)
        xrsl += "(ARC_STDOUT %s)(ARC_STDERR %s)" % (job['standardOutput'], job['standardError'])
        xrsl += ')'

        if "xrsl" in requirements:
            xrsl += requirements["xrsl"]

        # User supplied thingies:
        xrsl += self.user_xrsl
        if task['jobType']:
            for s in task['jobType'].split('&&'):
                if re.match('^ *\(.*=.*\) *$', s):
                    xrsl += s

        return xrsl


    def submit(self, task, requirements={}, config='', service = ''):
        """
        set up submission parameters and submit
        uses self.decode()

        return jobAttributes, bulkId, service

        - jobAttributs is a map of the format
              jobAttributes[ 'name' : 'schedulerId' ]
        - bulkId is an eventual bulk submission identifier (i.e. None for ARC)
        - service is a endpoit to connect with (such as the WMS)
        """
        assert type(task) == Task

        jobAttributes = {}
        bulkId = None

        # Build xRSL 
        xrsl = self.jobDescription(task, requirements, config, service)
        self.logging.debug("The xRSL code:\n%s" % xrsl)
        xrsl_file = "/tmp" + '/%s-jobs.xrsl' % task['name']
        f = open(xrsl_file, "w")
        f.write(xrsl)
        f.close()

        # Submit
        opt = get_arcsub_opts(requirements)
        command = self.pre_arcCmd + "arcsub %s %s" % (xrsl_file, opt)
        self.logging.debug(command)
        self.setTimeout(300)
        output, exitStat = self.ExecuteCommand(command)
        self.logging.debug("arcsub exitStatus: %i" % exitStat)
        self.logging.debug("arcsub output:\n" + output)
        os.remove(xrsl_file)


        # Parse arcsub output
        subRe = re.compile("job submitted with jobid: +(\w+://([a-zA-Z0-9.-]+)(:\d+)?(/.*)?/\w+)", flags=re.I)
        failRe = re.compile("the following .* were not submitted", flags=re.I)
        failed_names = []
        arcIds = []
        in_failed_list = False
        for line in output.split('\n'):
            if in_failed_list:
                name = line.split(': ')[1]
                failed_names.append(name)
                continue

            m = re.match(subRe, line)
            if m:
                arcIds.append(m.group(1))
            elif re.match(failRe, line):
                in_failed_list = True
            elif line.find("ERROR") >= 0:
                self.logging.warning("Found '%s' in arcsub output" % line)


        # Find job names
        i = 0
        for job in task.getJobs():
            name = job['name']

            if name in failed_names:
                msg = "Submitting job '%s' failed" % name
                self.logging.error(msg)
                job.runningJob.errors.append(msg)
                continue

            jobAttributes[name] = arcIds[i]
            self.logging.info("Submitted job %s with id %s" % (name, arcIds[i]))
            i += 1

        return jobAttributes, None, service 


    def createJobsFile(self, joblist, action = None):
        """
        Create a file with job arcIds.
        Return a file object, and an {arcId: job}-dictionary.
        The file will be removed when the file object is closed.
        """

        arcId2job = {}
        jobsFile = tempfile.NamedTemporaryFile(prefix="crabjobs.")

        for job in joblist:

            if not self.valid(job.runningJob):
                if not job.runningJob['schedulerId']:
                    self.logging.debug("job %s has no schedulerId!" % job['name'])
                self.logging.debug("job invalid: schedulerId = %s" % str(job.runningJob['schedulerId']))
                self.logging.debug("job invalid: closed = %s" % str(job.runningJob['closed']))
                self.logging.debug("job invalid: status = %s" % str(job.runningJob['status']))
                continue

            arcId = job.runningJob['schedulerId']
            if (action):
                self.logging.debug('%s job %s with arcId %s' % (action, job['name'], arcId))
            jobsFile.write(arcId + "\n")
            arcId2job[arcId] = job
        jobsFile.flush()

        return jobsFile, arcId2job



    def query(self, obj, service='', objType='node'):
        """
        Query status and eventually other scheduler related information,
        and store it in the job.runningJob data structure.

        It may use single 'node' scheduler id or bulk id for association

        """
        if type(obj) == Task:
            joblist = obj.jobs
        elif type(obj) == Job:
            joblist = [obj]
        else:
            raise SchedulerError('wrong argument type', str(type(obj)))

        jobsFile, arcId2job = self.createJobsFile(joblist, "Will query")

        if len(arcId2job) == 0:
            self.logging.info("No (valid) jobs to query")
            return

        cmd = self.pre_arcCmd + 'arcstat -i %s' % jobsFile.name
        output, stat = self.ExecuteCommand(cmd)
        self.logging.debug("arcstat (%i) said:\n%s" % (stat, output))
        jobsFile.close()
        if stat != 0:
            msg = '%i exit status for arcstat' % stat
            self.logging.error(msg)

        # Parse output of arcstat
        for jobstring in splitARCStatOutput(output):

            arcStat = None
            host = None
            jobExitCode = None
            arcId = None

            if jobstring.find("Job information not found") >= 0:
                if re.search("this job was.* recently submitted", jobstring, flags=re.I) >= 0:
                    arcStat = "ACCEPTING"  # At least approximately true
                else:
                    arcStat = "UNKNOWN"

                arcIdMatch = re.search("(\w+://([a-zA-Z0-9.-]+)\S*/\w*)", jobstring)
                if arcIdMatch:
                    arcId = arcIdMatch.group(1)
                    host = arcIdMatch.group(2)
            elif jobstring.find("Malformed URL:") >= 0:
                # This is something that really shoudln't happen.
                arcStat = "WTF?"

                arcIdMatch = re.search("URL: (\w+://([a-zA-Z0-9.-]+)\S*/\w*)", jobstring)
                if arcIdMatch:
                    arcId = arcIdMatch.group(1)
                    host = arcIdMatch.group(2)

            elif jobstring.find("Job not found in job list:") >= 0:
                arcIdMatch = re.search("(\w+://([a-zA-Z0-9.-]+)\S*/\w*)", jobstring)
                if arcIdMatch:
                    arcId = arcIdMatch.group(1)
                    host = arcIdMatch.group(2)
                arcStat = "UNKNOWN"
            else:

                # With special cases taken care of above, we are left with
                # "normal" jobs. They are assumed to have the format
                #
                # Job <arcId>
                #   Status: <status>
                #   Exit Code: <exit code>
                #
                # "Exit Code"-line might be missing.
                # Additional lines may exist, but we'll ignore them.

                for line in jobstring.split('\n'):

                    arcIdMatch = re.match("job:? +(\w+://([a-zA-Z0-9.-]+)\S*/\w*)", line, flags=re.I)
                    if arcIdMatch:
                        arcId = arcIdMatch.group(1)
                        host = arcIdMatch.group(2)
                        continue
                        
                    statusMatch = re.match(" +state: *[^(]*\((.+)\)", line, flags=re.I)
                    if statusMatch:
                        arcStat = statusMatch.group(1)
                        continue
                        
                    codeMatch = re.match(" +exit code: *(\d+)", line, flags=re.I)
                    if codeMatch:
                        jobExitCode = codeMatch.group(1)
                        continue

            if arcId:
                job = arcId2job[arcId]
                if arcStat:
                    job.runningJob['statusScheduler'] = Arc2StatusScheduler[arcStat]
                    job.runningJob['status'] = Arc2Status[arcStat]
                    job.runningJob['statusReason'] = Arc2StatusReason[arcStat]
                if host:
                    job.runningJob['destination'] = host
                if jobExitCode:
                    job.runningJob['wrapperReturnCode'] = jobExitCode
            else:
                self.logging.debug("Huh? No arcId! '%s'" % jobstring)
                

        return


    def getOutput(self, obj, outdir=''):
        """
        Get output files from jobs in 'obj' and put them in 'outdir', and  
        remove the job from the CE.
        """
        if type(obj) == Task:
            self.logging.debug("getOutput called for %i jobs" % len(obj.jobs))
            joblist = obj.jobs
            if outdir == '':
                outdir = obj['outputDirectory']
        elif type(obj) == Job:
            self.logging.debug("getOutput called for 1 job")
            joblist = [obj]
        else:
            raise SchedulerError('wrong argument type', str(type(obj)))

        assert outdir != ''
        if outdir[-1] != '/': outdir += '/'

        for job in joblist:
            tmpdir = tempfile.mkdtemp(prefix="joboutputs.", dir=outdir)
            
            cmd = self.pre_arcCmd + 'arcget --timeout=600 %s --dir %s' % (job.runningJob['schedulerId'], tmpdir)
            self.logging.debug("Running command: %s" % cmd)
            output, stat = self.ExecuteCommand(cmd)
            self.logging.debug("Status and output of arcget: %i, '%s'" % (stat, output))
            if stat != 0:
                msg = "arcget failed with status %i: %s" % (stat, output)
                self.logging.warning(msg)
            else:
                # Copy the dowlodaed files to their final destination
                cmd = 'mv %s/*/* %s' % (tmpdir, outdir)
                self.logging.debug("Moving files from %s/* to %s" % (tmpdir, outdir))
                output, stat = self.ExecuteCommand(cmd)
                if stat != 0:
                    msg = "Moving files to final destination failed: %s" % (output)
                    self.logging.warning(msg)
                else:
                    cmd = ' rm -r %s' % (tmpdir)
                    self.logging.debug("Removing tempdir %s" % (tmpdir))
                    output, stat = self.ExecuteCommand(cmd)
                    if stat != 0:
                        msg = "Removing tempdir: %s" % (output)
                        self.logging.warning(msg)





    def kill(self, obj):
        """
        Kill the job instance
        """
        if type(obj) == Job:
            jobList = [obj]
        elif type(obj) == Task:
            jobList = obj.jobs
        else:
            raise SchedulerError('wrong argument type', str(type(obj)))

        jobsFile, arcId2job = self.createJobsFile(jobList, "Will kill")

        cmd = self.pre_arcCmd + "arckill -i " + jobsFile.name
        output, stat = self.ExecuteCommand(cmd)
        if stat != 0:
            raise SchedulerError('arckill returned %i' % stat, output, cmd)

        for line in output.split('\n'):
            # If a job URL ("arcId") occurs on a line of output, it tends
            # to be en error message:
            errorMatch = re.match(".*: *(gsiftp://[a-zA-Z0-9.-]+\S*/\w*)", line)
            if errorMatch:
                arcId = errorMatch.group(1)
                job = arcId2job[arcId]
                job.runningJob.errors.append("Killing job %s failed: %s" % (job['name'], line))


    def postMortem (self, obj, arcId, outfile, service):
        """
        execute any post mortem command such as logging-info
        and write it in outfile
        """
        self.logging.debug('postMortem for job %s' % arcId)
        cmd = self.pre_arcCmd + "arccat -l " + arcId + " > " + outfile
        return self.ExecuteCommand(cmd)[0]


    def matchResources(self, obj, requirements='', config='', service=''):
        """
        perform a resources discovery
        returns a list of resulting sites
        """
        raise NotImplementedError


    def getClusters(self):
        """
        Get a list of clusters from the ARC info.sys., including the
        installed RTE:s and local SE:s of the clusters.
        """

        cmd = self.pre_arcCmd + 'arcinfo -l'
        self.logging.debug("Running command '%s'" % cmd)
        output, s = self.ExecuteCommand(cmd)

        clusters = []
        for c_text in ARCInfoSplitClusters(output):
            c = {}
            c["cluster"] = None
            c["rte"] = []
            in_rtelist = False
            for line in c_text:
                if in_rtelist:
                    if line == "":
                        in_rtelist = False
                    else:
                        c["rte"].append(line.lstrip())
                if not in_rtelist:
                    if line.find("Installed application environments:") >= 0:
                        in_rtelist = True
                    elif not c["cluster"] and line.find("Name:") >= 0:
                        c["cluster"] = line.split(': ')[1]

            if c["cluster"]: clusters.append(c)
        return clusters



    def check_CEs(self, CEs, tags, vos, seList, blacklist, whitelist, check_RTEs, full):
        """
        Return those CEs that fullfill requirements.
        """

        accepted_CEs = []

        for ce in CEs:
            name = ce['cluster']
            #localSEs = set(ce['localse'])
            RTEs = set(ce['rte'])

            #if count_nonempty(seList) > 0 and not set(seList) & localSEs:
            #    if count_nonempty(whitelist) > 0 and name in whitelist:
            #        self.logging.warning("NOTE: Whitelisted CE %s was found but isn't close to any SE that have the data" % name)
            #    continue

            if check_RTEs and count_nonempty(tags) > 0  and not set(tags) <= RTEs:
                if count_nonempty(whitelist) > 0 and name in whitelist:
                    self.logging.warning("NOTE: Whitelisted CE %s was found but doesn't have all required runtime environments installed" % name)
                continue

            if count_nonempty(blacklist) > 0 and name in blacklist:
                continue

            if count_nonempty(whitelist) > 0 and name not in whitelist:
                continue

            accepted_CEs.append(name)
            #if not full:
            #    break

        return accepted_CEs

    def delegateProxy(self, wms = ''):
        # ARC doesn't need anything to be done here.
        return

    def lcgInfo(self, tags, vos, seList=None, blacklist=None, whitelist=None, full=False):
        """
        Query grid information system for CE:s.
        Returns a list of resulting sites (or the first one, if full == False)
        """
        # FIXME: Currently we ignore 'vos'!

        self.logging.debug("lcgInfo called with %s, %s, %s, %s, %s, %s" % (str(tags), str(vos), str(seList), str(blacklist), str(whitelist), str(full)))

        if self.accepted_CEs:
            self.logging.debug("lcgInfo: using cached result")
            return self.accepted_CEs
            
        if type(full) == type(""):  
            full = (full == "True")

        CEs = self.getClusters()
        self.logging.info("ARC info.sys. found %i clusters in total ..." % len(CEs))
        if CEs:
            self.accepted_CEs = self.check_CEs(CEs, tags, vos, seList, blacklist, whitelist, check_RTEs=True, full=full)
            self.logging.info("... of which %i fulfill our requirements" % len(self.accepted_CEs))
        else:
            # Failsafe mode:
            self.logging.warning("Didn't get any clusters from the info sys. Relying on static list of clusters instead")
            CEs = [{'cluster': 'jade-cms.hip.fi', 'rte': []},
                   {'cluster': 'korundi.grid.helsinki.fi', 'rte': []},
                   {'cluster': 'alcyone-cms.grid.helsinki.fi', 'rte': []},
                   {'cluster': 'nodeslab-0002.nlab.tb.hiit.fi', 'rte': []}]
            self.accepted_CEs = self.check_CEs(CEs, tags, vos, seList, blacklist, whitelist, check_RTEs=False, full=full)
        self.logging.debug("Accepted clusters: " + str(self.accepted_CEs))
        return self.accepted_CEs
