import re
import sys
import time
import types
import fcntl
import random
import datetime
import commands
import ErrorCode
import broker_util
import PandaSiteIDs
from taskbuffer import ProcessGroups
from config import panda_config

from pandalogger.PandaLogger import PandaLogger
_log = PandaLogger().getLogger('broker')

# all known sites
_allSites = PandaSiteIDs.PandaSiteIDs.keys()
        
# sites for prestaging
#prestageSites = ['BNL_ATLAS_test','BNL_ATLAS_1','BNL_ATLAS_2']

# non LRC checking
_disableLRCcheck = []

# lock for uuidgen
_lockGetUU   = open(panda_config.lockfile_getUU, 'w')

# short-long mapping
shortLongMap = {'ANALY_BNL_ATLAS_1':'ANALY_LONG_BNL_ATLAS',
                'ANALY_LYON-T2'    :'ANALY_LONG_LYON-T2',
                'ANALY_LYON_DCACHE':'ANALY_LONG_LYON_DCACHE',
                }

# processingType to skip brokerage
skipBrokerageProTypes = ['prod_test']

# comparison function for sort
def _compFunc(jobA,jobB):
    # append site if not in list
    if not jobA.computingSite in _allSites:
        _allSites.append(jobA.computingSite)
    if not jobB.computingSite in _allSites:
        _allSites.append(jobB.computingSite)
    # compare
    indexA = _allSites.index(jobA.computingSite) 
    indexB = _allSites.index(jobB.computingSite) 
    if indexA > indexB:
        return 1
    elif indexA < indexB:
        return -1
    else:
        return 0


# release checker
def _checkRelease(jobRels,siteRels):
    # all on/off
    if "True" in siteRels:
        return True
    if "False" in siteRels:
        return False
    # loop over all releases
    for tmpRel in jobRels.split('\n'):
        relVer = re.sub('^Atlas-','',tmpRel)
        # not available releases
        if not relVer in siteRels:
            return False
    return True


# get list of files which already exist at the site
def _getOkFiles(v_ce,v_files,v_guids,allLFNs,allGUIDs,allOkFilesMap):
    # DQ2 URL
    dq2URL = v_ce.dq2url
    dq2IDs = v_ce.setokens.keys()
    try:
        dq2IDs.remove('')
    except:
        pass
    dq2IDs.sort()
    if dq2IDs == []:
        dq2ID = v_ce.ddm
    else:
        dq2ID = ''
        for tmpID in dq2IDs:
            dq2ID += '%s,' % tmpID
        dq2ID = dq2ID[:-1]    
    # set LFC and SE name 
    tmpSE = []
    if not v_ce.lfchost in [None,'']:
        dq2URL = 'lfc://'+v_ce.lfchost+':/grid/atlas/'
        tmpSE  = broker_util.getSEfromSched(v_ce.se)
    # use bulk lookup
    if allLFNs != []:
        # get bulk lookup data
        if not allOkFilesMap.has_key(dq2ID):
            # get files from LRC
            allOkFilesMap[dq2ID] = broker_util.getFilesFromLRC(allLFNs,dq2URL,guids=allGUIDs,
                                                               storageName=tmpSE,getPFN=True)
        # make return map
        retMap = {}
        for tmpLFN in v_files:
            if allOkFilesMap[dq2ID].has_key(tmpLFN):
                retMap[tmpLFN] = allOkFilesMap[dq2ID][tmpLFN]
        # return
        return retMap
    else:
        # old style
        return broker_util.getFilesFromLRC(v_files,dq2URL,guids=v_guids,
                                           storageName=tmpSE,getPFN=True)


# check reprocessing or not
def _isReproJob(tmpJob):
    if tmpJob != None:
        if tmpJob.processingType in ['reprocessing']:
            return True
        if tmpJob.transformation in ['csc_cosmics_trf.py','csc_BSreco_trf.py','BStoESDAODDPD_trf.py']:
            return True
    return False


    
# set 'ready' if files are already there
def _setReadyToFiles(tmpJob,okFiles,siteMapper):
    allOK = True
    tmpSiteSpec = siteMapper.getSite(tmpJob.computingSite)
    tmpSrcSpec  = siteMapper.getSite(siteMapper.getCloud(tmpJob.cloud)['source'])
    _log.debug(tmpSiteSpec.seprodpath)
    prestageSites = getPrestageSites(siteMapper)
    for tmpFile in tmpJob.Files:
        if tmpFile.type == 'input':
            if (tmpJob.computingSite.endswith('_REPRO') or tmpJob.computingSite == siteMapper.getCloud(tmpJob.cloud)['source'] \
                or tmpSiteSpec.ddm == tmpSrcSpec.ddm) \
                   and (not tmpJob.computingSite in prestageSites):
                # EGEE T1. use DQ2 prestage only for on-tape files
                if tmpSiteSpec.seprodpath.has_key('ATLASDATATAPE') and tmpSiteSpec.seprodpath.has_key('ATLASMCTAPE') and \
                       okFiles.has_key(tmpFile.lfn):
                    tapeOnly = True
                    tapeCopy = False
                    for okPFN in okFiles[tmpFile.lfn]:
                        if re.search(tmpSiteSpec.seprodpath['ATLASDATATAPE'],okPFN) == None and \
                               re.search(tmpSiteSpec.seprodpath['ATLASMCTAPE'],okPFN) == None:
                            # there is a disk copy
                            if tmpJob.cloud == 'US':
                                # check for BNLPANDA
                                if (tmpSiteSpec.seprodpath.has_key('ATLASMCDISK') and \
                                    re.search(tmpSiteSpec.seprodpath['ATLASMCDISK'],okPFN) != None) or \
                                    (tmpSiteSpec.seprodpath.has_key('ATLASDATADISK') and
                                       re.search(tmpSiteSpec.seprodpath['ATLASDATADISK'],okPFN) != None):
                                    tapeOnly = False
                            else:
                                tapeOnly = False
                        else:
                            # there is a tape copy
                            tapeCopy = True
                    # trigger prestage when disk copy doesn't exist or token is TAPE
                    if tapeOnly or (tapeCopy and tmpFile.dispatchDBlockToken in ['ATLASDATATAPE','ATLASMCTAPE']):
                        allOK = False
                    else:
                        # set ready                        
                        tmpFile.status = 'ready'
                        tmpFile.dispatchDBlock = 'NULL'
                else:
                    # set ready anyway even if LFC is down. i.e. okFiles doesn't contain the file
                    tmpFile.status = 'ready'
                    tmpFile.dispatchDBlock = 'NULL'                                
            elif (((tmpFile.lfn in okFiles) or (tmpJob.computingSite == tmpJob.destinationSE)) \
                     and (not tmpJob.computingSite in prestageSites or \
                          (tmpJob.computingSite in prestageSites and not tmpJob.cloud in ['US']))) \
                  or tmpFile.status == 'missing':
                # don't use TAPE replicas when T1 is used as T2
                if okFiles.has_key(tmpFile.lfn) and \
                       tmpSiteSpec.seprodpath.has_key('ATLASDATATAPE') and len(okFiles[tmpFile.lfn]) == 1 and \
                       re.search(tmpSiteSpec.seprodpath['ATLASDATATAPE'],okFiles[tmpFile.lfn][0]) != None:
                    allOK = False
                else:
                    # set ready if the file exists and the site doesn't use prestage
                    tmpFile.status = 'ready'
                    tmpFile.dispatchDBlock = 'NULL'
            else:
                # prestage with PandaMover
                allOK = False
    # unset disp dataset
    if allOK:
        tmpJob.dispatchDBlock = 'NULL'
        
    

# check number/size of inputs
def _isTooManyInput(nFilesPerJob,inputSizePerJob):
    # the number of inputs is larger than 5 or
    # size of inputs is larger than 500MB
    if nFilesPerJob > 5 or inputSizePerJob > 500*1024*1024:
        return True
    return False


# send analysis brokerage info
def sendAnalyBrokeageInfo(results,prevRelease,diskThreshold,chosenSite,prevCmtConfig,
                          siteReliability):
    # send log messages
    messageList = []
    for resultType,resultList in results.iteritems():
        for resultItem in resultList:
            if resultType == 'rel':
                if prevCmtConfig in ['','NULL',None]:
                    msgBody = 'action=skip site=%s reason=missingapp - app=%s is missing' % (resultItem,prevRelease)
                else:
                    msgBody = 'action=skip site=%s reason=missingapp - app=%s/%s is missing' % (resultItem,prevRelease,prevCmtConfig)
            elif resultType == 'pilot':
                msgBody = 'action=skip site=%s reason=nopilot - no pilots for last 3 hours' % resultItem
            elif resultType == 'disk':
                msgBody = 'action=skip site=%s reason=diskshortage - disk shortage < %sGB' % (resultItem,diskThreshold)
            elif resultType == 'memory':
                msgBody = 'action=skip site=%s reason=ramshortage - RAM shortage' % resultItem
            elif resultType == 'maxtime':
                msgBody = 'action=skip site=%s reason=maxtime - shorter walltime limit' % resultItem
            elif resultType == 'status':
                msgBody = 'action=skip site=%s reason=sitestatus - not online' % resultItem 
            elif resultType == 'reliability':
                msgBody = 'action=skip site=%s reason=reliability - insufficient>%s' % (resultItem ,siteReliability)
            elif resultType == 'weight':
                tmpSite,tmpWeight = resultItem
                if tmpSite == chosenSite:
                    msgBody = 'action=choose site=%s reason=maxweight - max weight=%s' % (tmpSite,tmpWeight)
                else:
                    msgBody = 'action=skip site=%s reason=notmaxweight - weight=%s' % (tmpSite,tmpWeight)
            elif resultType == 'prefcountry':
                tmpSite,tmpCountry = resultItem
                if tmpSite == chosenSite:
                    msgBody = 'action=prefer country=%s reason=countrygroup - preferential brokerage for beyond-pledge' % tmpCountry
                else:
                    continue
            else:
                continue
            messageList.append(msgBody)
    # return
    return messageList


# send analysis brokerage info to logger
def sendMsgToLogger(message):
    _log.debug(message)


# send analysis brokerage info to logger with HTTP
def sendMsgToLoggerHTTP(msgList,job):
    try:
        # logging
        iMsg = 0
        # message type
        msgType = 'analy_brokerage'
        # make header
        if not job.jobsetID in [None,'NULL']:
            msgHead = "dn='%s' : jobset=%s jobdef=%s" % (job.prodUserName,job.jobsetID,job.jobDefinitionID)
        else:
            msgHead = "dn='%s' : jobdef=%s" % (job.prodUserName,job.jobDefinitionID)
        for msgBody in msgList:
            # make message
            message = msgHead + ' : ' + msgBody
            # dump locally
            _log.debug(message)
            # get logger
            _pandaLogger = PandaLogger()            
            _pandaLogger.lock()
            _pandaLogger.setParams({'Type':msgType})
            logger = _pandaLogger.getHttpLogger(panda_config.loggername)
            # add message
            logger.info(message)
            # release HTTP handler
            _pandaLogger.release()
            # sleep
            iMsg += 1
            if iMsg % 5 == 0:
                time.sleep(1)
    except:
        errType,errValue = sys.exc_info()[:2]
        _log.error("sendMsgToLoggerHTTP : %s %s" % (errType,errValue))
    

# get T2 candidates when files are missing at T2
def getT2CandList(tmpJob,siteMapper,t2FilesMap):
    if tmpJob == None:
        return []
    # no cloud info
    if not t2FilesMap.has_key(tmpJob.cloud):
        return []
    # loop over all files
    tmpCandT2s = None
    for tmpFile in tmpJob.Files:
        if tmpFile.type == 'input' and tmpFile.status == 'missing':
            # no dataset info
            if not t2FilesMap[tmpJob.cloud].has_key(tmpFile.dataset):
                return []
            # initial candidates
            if tmpCandT2s == None:
                tmpCandT2s = t2FilesMap[tmpJob.cloud][tmpFile.dataset]['sites']
            # check all candidates
            newCandT2s = []
            for tmpCandT2 in tmpCandT2s:
                # site doesn't have the dataset
                if not t2FilesMap[tmpJob.cloud][tmpFile.dataset]['sites'].has_key(tmpCandT2):
                    continue
                # site has the file
                if tmpFile.lfn in t2FilesMap[tmpJob.cloud][tmpFile.dataset]['sites'][tmpCandT2]:
                    if not tmpCandT2 in newCandT2s:
                        newCandT2s.append(tmpCandT2)
            # set new candidates
            tmpCandT2s = newCandT2s
            if tmpCandT2s == []:
                break
    # return [] if no missing files         
    if tmpCandT2s == None:
        return []
    # return
    tmpCandT2s.sort() 
    return tmpCandT2s 


# get hospital queues
def getHospitalQueues(siteMapper):
    retMap = {}
    # hospital words
    goodWordList = ['CORE$','VL$','MEM$','MP\d+$','LONG$']
    # loop over all clouds
    for tmpCloudName in siteMapper.getCloudList():
        # get cloud
        tmpCloudSpec = siteMapper.getCloud(tmpCloudName)
        # get T1
        tmpT1Name = tmpCloudSpec['source']
        tmpT1Spec = siteMapper.getSite(tmpT1Name)
        # skip if DDM is undefined
        if tmpT1Spec.ddm == []:
            continue
        # loop over all sites
        for tmpSiteName in tmpCloudSpec['sites']:
            # skip T1 defined in cloudconfig
            if tmpSiteName == tmpT1Name:
                continue
            # check hospital words
            checkHospWord = False
            for tmpGoodWord in goodWordList:
                if re.search(tmpGoodWord,tmpSiteName) != None:
                    checkHospWord = True
                    break
            if not checkHospWord:
                continue
            # check site
            if not siteMapper.checkSite(tmpSiteName):
                continue
            tmpSiteSpec = siteMapper.getSite(tmpSiteName)
            # check DDM
            if tmpT1Spec.ddm == tmpSiteSpec.ddm:
                # append
                if not retMap.has_key(tmpCloudName):
                    retMap[tmpCloudName] = []
                if not tmpSiteName in retMap[tmpCloudName]:
                    retMap[tmpCloudName].append(tmpSiteName)
    _log.debug('hospital queues : %s' % str(retMap))
    # return
    return retMap


# get prestage sites
def getPrestageSites(siteMapper):
    retList = []
    # get cloud
    tmpCloudSpec = siteMapper.getCloud('US')
    # get T1
    tmpT1Name = tmpCloudSpec['source']
    tmpT1Spec = siteMapper.getSite(tmpT1Name)
    # loop over all sites
    for tmpSiteName in tmpCloudSpec['sites']:
        # check site
        if not siteMapper.checkSite(tmpSiteName):
            continue
        # get spec
        tmpSiteSpec = siteMapper.getSite(tmpSiteName)
        # add if DDM is the same as T1
        if tmpT1Spec.ddm == tmpSiteSpec.ddm and not tmpSiteName in retList:
            retList.append(tmpSiteName)
    _log.debug('US prestage sites : %s' % str(retList))            
    # return
    return retList


# make compact dialog message
def makeCompactDiagMessage(header,results):
    # limit
    maxSiteList  = 5
    # types for compact format
    compactTypeList = ['status','cpucore']
    # message mapping
    messageMap = {'rel'          : 'missing rel/cache',
                  'pilot'        : 'no pilot', 
                  'status'       : 'not online',
                  'disk'         : 'SE full',
                  'memory'       : 'RAM shortage',
                  'transferring' : 'many transferring',
                  'share'        : 'zero share',
                  'maxtime'      : 'short walltime',
                  'cpucore'      : 'CPU core mismatch',
                  'scratch'      : 'small scratch disk'
                  }
    # put header
    if header in ['',None]:
        retStr = 'No candidate - '
    else:
        retStr = 'special brokerage for %s - ' % header
    # count number of sites per type
    numTypeMap = {}
    for resultType,resultList in results.iteritems():
        # ignore empty
        if len(resultList) == 0:
            continue
        # add
        nSites = len(resultList)
        if not numTypeMap.has_key(nSites):
            numTypeMap[nSites] = []
        numTypeMap[nSites].append(resultType)    
    # sort
    numTypeKeys = numTypeMap.keys()
    numTypeKeys.sort()
    # use compact format for largest one
    largeTypes = None
    if len(numTypeKeys) > 0:
        largeTypes = numTypeMap[numTypeKeys[-1]]
    # loop over all types
    for numTypeKey in numTypeKeys:
        for resultType in numTypeMap[numTypeKey]:
            # label
            if messageMap.has_key(resultType):
                retStr += '%s at ' % messageMap[resultType]
            else:
                retStr += '%s at' % resultType
            # use comact format or not
            if (resultType in compactTypeList+largeTypes \
               or len(results[resultType]) >= maxSiteList) \
               and header in ['',None,'reprocessing'] :
                if len(results[resultType]) == 1:
                    retStr += '%s site' % len(results[resultType])
                else:
                    retStr += '%s sites' % len(results[resultType])
            else:
                for tmpSite in results[resultType]:
                    retStr += '%s,' % tmpSite
                retStr = retStr[:-1]
            retStr += '. '
    retStr = retStr[:-2]
    # return
    return retStr


# message class
class MsgWrapper:
    def __init__(self):
        self.timestamp = datetime.datetime.utcnow().isoformat('/')

    def info(self,msg):
        _log.info(self.timestamp + ' ' + msg)

    def debug(self,msg):
        _log.debug(self.timestamp + ' ' + msg)

    def error(self,msg):
        _log.error(self.timestamp + ' ' + msg)

    def warning(self,msg):
        _log.warning(self.timestamp + ' ' + msg)

        

# schedule
def schedule(jobs,taskBuffer,siteMapper,forAnalysis=False,setScanSiteList=[],trustIS=False,
             distinguishedName=None,specialWeight={},getWeight=False,sizeMapForCheck={},
             datasetSize=0,replicaMap={},pd2pT1=False,reportLog=False,minPriority=None,
             t2FilesMap={},preferredCountries=[],siteReliability=None):
    # make a message instance
    tmpLog = MsgWrapper()
    try:
        tmpLog.debug('start %s %s %s %s minPrio=%s pref=%s siteRel=%s' % (forAnalysis,str(setScanSiteList),trustIS,
                                                                          distinguishedName,minPriority,
                                                                          str(preferredCountries),
                                                                          siteReliability))
        if specialWeight != {}:
            tmpLog.debug('PD2P weight : %s' % str(specialWeight))
        tmpLog.debug('replicaMap : %s' % str(replicaMap))
        # no jobs
        if len(jobs) == 0:
            tmpLog.debug('finished : no jobs')        
            return
        allOkFilesMap = {}
        # use ANALY_CERN_XROOTD and not ANALY_CERN for EOS migration
        if forAnalysis:
            if 'ANALY_CERN_XROOTD' in setScanSiteList and 'ANALY_CERN' in setScanSiteList:
                setScanSiteList.remove('ANALY_CERN')
                tmpLog.debug('remove ANALY_CERN since ANALY_CERN_XROOTD is also a candidate')
        nJob  = 20
        iJob  = 0
        nFile = 20
        fileList = []
        guidList = []
        okFiles = {}
        totalNumInputs = 0
        totalInputSize = 0
        chosen_ce      = None
        prodDBlock     = None
        computingSite  = None
        dispatchDBlock = None
        previousCloud  = None
        prevRelease    = None
        prevMemory     = None
        prevCmtConfig  = None
        prevProType    = None
        prevSourceLabel= None
        prevDiskCount  = None
        prevHomePkg    = None
        prevDirectAcc  = None
        prevCoreCount  = None
        prevBrokergageSiteList = None
        prevManualPreset = None
        prevGoToT2Flag   = None
        prevWorkingGroup = None
        prevMaxCpuCount  = None
        prevBrokerageNote = None
        prevPriority      = None

        nWNmap = {}
        indexJob = 0
        vomsOK = None

        diskThreshold     = 200
        diskThresholdPD2P = 1024 * 3
        manyInputsThr     = 20
        weightUsedByBrokerage = {}

        prestageSites = getPrestageSites(siteMapper)

        # get statistics
        faresharePolicy = {}
        newJobStatWithPrio = {}
        jobStatBrokerCloudsWithPrio = {}
        if len(jobs) > 0 and (jobs[0].processingType.startswith('gangarobot') or \
                              jobs[0].processingType.startswith('hammercloud') or \
                              jobs[0].processingType in ['pandamover','usermerge']):
            # disable redundant counting for HC
            jobStatistics = {}
            jobStatBroker = {}
            jobStatBrokerClouds = {}
            nRunningMap = {}
            hospitalQueueMap = {}
        else:
            jobStatistics = taskBuffer.getJobStatistics(forAnal=forAnalysis)
            if not forAnalysis:
                jobStatBroker = {}
                jobStatBrokerClouds = taskBuffer.getJobStatisticsBrokerage()
                faresharePolicy = taskBuffer.getFaresharePolicy()
            else:
                if minPriority == None:
                    jobStatBroker = taskBuffer.getJobStatisticsAnalBrokerage()
                else:
                    jobStatBroker = taskBuffer.getJobStatisticsAnalBrokerage(minPriority=minPriority)                    
                nRunningMap   = taskBuffer.getnRunningInSiteData()
            hospitalQueueMap = getHospitalQueues(siteMapper)
        # sort jobs by siteID. Some jobs may already define computingSite
        jobs.sort(_compFunc)
        # brokerage for analysis 
        candidateForAnal = True
        relCloudMap      = {}
        loggerMessages   = []
        # get all input files for bulk LFC lookup  
        allLFNs  = []
        allGUIDs = []
        for tmpJob in jobs:
            if tmpJob.prodSourceLabel in ('test','managed'):
                for tmpFile in tmpJob.Files:
                    if tmpFile.type == 'input' and not tmpFile.lfn in allLFNs:
                        allLFNs.append(tmpFile.lfn)
                        allGUIDs.append(tmpFile.GUID)
        # loop over all jobs + terminator(None)
        for job in jobs+[None]:
            indexJob += 1
            # ignore failed jobs
            if job == None:
                pass
            elif job.jobStatus == 'failed':
                continue
            # list of sites for special brokerage
            specialBrokergageSiteList = []
            # note for brokerage
            brokerageNote = ''
            # send jobs to T2 when files are missing at T1
            goToT2Flag = False
            if job != None and job.computingSite == 'NULL' and job.prodSourceLabel in ('test','managed') \
                   and specialBrokergageSiteList == []:
                currentT2CandList = getT2CandList(job,siteMapper,t2FilesMap)
                if currentT2CandList != []:
                    goToT2Flag = True
                    specialBrokergageSiteList = currentT2CandList
                    tmpLog.debug('PandaID:%s -> set SiteList=%s to use T2 for missing files at T1' % (job.PandaID,specialBrokergageSiteList))
                    brokerageNote = 'useT2'
            # hack for split T1
            if job != None and job.computingSite == 'NULL' and job.prodSourceLabel in ('test','managed') \
               and job.cloud == 'NL' and specialBrokergageSiteList == []:
                # loop over all input datasets
                tmpCheckedDS = []
                useSplitT1 = None
                for tmpFile in job.Files:
                    if tmpFile.type == 'input' and (not tmpFile.dataset.startswith('ddo')) \
                       and (not tmpFile.dataset in tmpCheckedDS):
                        # init
                        if useSplitT1 == None:
                            useSplitT1 = True
                        # no replica map
                        if not replicaMap.has_key(tmpFile.dataset):
                            # not set
                            useSplitT1 = False
                            break
                        # check if input datasets are available only at NIKHEF
                        tmpRepMap = replicaMap[tmpFile.dataset]
                        splitT1HasDS = False
                        for tmpSplitT1Key in tmpRepMap.keys():
                            if tmpSplitT1Key.startswith('NIKHEF-ELPROD'):
                                splitT1HasDS = True
                                break
                        if splitT1HasDS \
                               and not tmpRepMap.has_key('SARA-MATRIX_MCDISK') \
                               and not tmpRepMap.has_key('SARA-MATRIX_DATADISK') \
                               and not tmpRepMap.has_key('SARA-MATRIX_MCTAPE') \
                               and not tmpRepMap.has_key('SARA-MATRIX_DATATAPE'):
                            pass
                        else:
                            # not set
                            useSplitT1 = False
                            break
                # set
                if useSplitT1 == True:
                    specialBrokergageSiteList = ['NIKHEF-ELPROD']
                    tmpLog.debug('PandaID:%s -> set SiteList=%s for split T1' % (job.PandaID,specialBrokergageSiteList))
                    brokerageNote = 'useSplitNLT1'                    
            # set computingSite to T1 for high priority jobs
            if job != None and job.currentPriority >= 950 and job.computingSite == 'NULL' \
                   and job.prodSourceLabel in ('test','managed') and specialBrokergageSiteList == []:
                specialBrokergageSiteList = [siteMapper.getCloud(job.cloud)['source']]
                # set site list to use T1 and T1_VL
                if hospitalQueueMap.has_key(job.cloud):
                    specialBrokergageSiteList += hospitalQueueMap[job.cloud]
                tmpLog.debug('PandaID:%s -> set SiteList=%s for high prio' % (job.PandaID,specialBrokergageSiteList))
                brokerageNote = 'highPrio'
            # set computingSite to T1 when too many inputs are required
            if job != None and job.computingSite == 'NULL' and job.prodSourceLabel in ('test','managed') \
                   and specialBrokergageSiteList == []:
                # counts # of inputs
                tmpTotalInput = 0
                for tmpFile in job.Files:
                    if tmpFile.type == 'input':
                        tmpTotalInput += 1
                if tmpTotalInput >= manyInputsThr:
                    specialBrokergageSiteList = [siteMapper.getCloud(job.cloud)['source']]
                    # set site list to use T1 and T1_VL
                    if hospitalQueueMap.has_key(job.cloud):
                        specialBrokergageSiteList += hospitalQueueMap[job.cloud]
                    tmpLog.debug('PandaID:%s -> set SiteList=%s for too many inputs' % (job.PandaID,specialBrokergageSiteList))
                    brokerageNote = 'manyInput'
            # use limited sites for reprocessing
            if job != None and job.computingSite == 'NULL' and job.prodSourceLabel in ('test','managed') \
                   and job.processingType in ['reprocessing'] and specialBrokergageSiteList == []:
                for tmpSiteName in siteMapper.getCloud(job.cloud)['sites']:
                    if siteMapper.checkSite(tmpSiteName):
                        tmpSiteSpec = siteMapper.getSite(tmpSiteName)
                        if _checkRelease(job.AtlasRelease,tmpSiteSpec.validatedreleases):
                            specialBrokergageSiteList.append(tmpSiteName)
                tmpLog.debug('PandaID:%s -> set SiteList=%s for processingType=%s' % (job.PandaID,specialBrokergageSiteList,job.processingType))
                brokerageNote = '%s' % job.processingType                
            # use limited sites for MP jobs
            if job != None and job.computingSite == 'NULL' and job.prodSourceLabel in ('test','managed') \
                   and not job.coreCount in [None,'NULL'] and job.coreCount > 1 and specialBrokergageSiteList == []:
                for tmpSiteName in siteMapper.getCloud(job.cloud)['sites']:
                    if siteMapper.checkSite(tmpSiteName):
                        tmpSiteSpec = siteMapper.getSite(tmpSiteName)
                        if tmpSiteSpec.coreCount > 1:
                            specialBrokergageSiteList.append(tmpSiteName)
                tmpLog.debug('PandaID:%s -> set SiteList=%s for MP=%scores' % (job.PandaID,specialBrokergageSiteList,job.coreCount))
                brokerageNote = 'MP=%score' % job.coreCount
            # manually set site
            manualPreset = False
            if job != None and job.computingSite != 'NULL' and job.prodSourceLabel in ('test','managed') \
                   and specialBrokergageSiteList == []:
                specialBrokergageSiteList = [job.computingSite]
                manualPreset = True
                brokerageNote = 'presetSite'
            overwriteSite = False
            # new bunch or terminator
            if job == None or len(fileList) >= nFile \
                   or (dispatchDBlock == None and job.homepackage.startswith('AnalysisTransforms')) \
                   or prodDBlock != job.prodDBlock or job.computingSite != computingSite or iJob > nJob \
                   or previousCloud != job.cloud or prevRelease != job.AtlasRelease \
                   or prevCmtConfig != job.cmtConfig \
                   or (computingSite in ['RAL_REPRO','INFN-T1_REPRO'] and len(fileList)>=2) \
                   or (prevProType in skipBrokerageProTypes and iJob > 0) \
                   or prevDirectAcc != job.transferType \
                   or prevMemory != job.minRamCount \
                   or prevDiskCount != job.maxDiskCount \
                   or prevCoreCount != job.coreCount \
                   or prevWorkingGroup != job.workingGroup \
                   or prevProType != job.processingType \
                   or prevMaxCpuCount != job.maxCpuCount \
                   or prevBrokergageSiteList != specialBrokergageSiteList:
                if indexJob > 1:
                    tmpLog.debug('new bunch')
                    tmpLog.debug('  iJob           %s'    % iJob)
                    tmpLog.debug('  cloud          %s' % previousCloud)
                    tmpLog.debug('  rel            %s' % prevRelease)
                    tmpLog.debug('  sourceLabel    %s' % prevSourceLabel)
                    tmpLog.debug('  cmtConfig      %s' % prevCmtConfig)
                    tmpLog.debug('  memory         %s' % prevMemory)
                    tmpLog.debug('  priority       %s' % prevPriority)
                    tmpLog.debug('  prodDBlock     %s' % prodDBlock)
                    tmpLog.debug('  computingSite  %s' % computingSite)
                    tmpLog.debug('  processingType %s' % prevProType)
                    tmpLog.debug('  workingGroup   %s' % prevWorkingGroup)
                    tmpLog.debug('  coreCount      %s' % prevCoreCount)
                    tmpLog.debug('  maxCpuCount    %s' % prevMaxCpuCount)
                    tmpLog.debug('  transferType   %s' % prevDirectAcc)
                    tmpLog.debug('  goToT2         %s' % prevGoToT2Flag)
                # brokerage decisions    
                resultsForAnal   = {'rel':[],'pilot':[],'disk':[],'status':[],'weight':[],'memory':[],
                                    'share':[],'transferring':[],'prefcountry':[],'cpucore':[],
                                    'reliability':[],'maxtime':[],'scratch':[]}
                # determine site
                if (iJob == 0 or chosen_ce != 'TOBEDONE') and prevBrokergageSiteList in [None,[]]:
                     # file scan for pre-assigned jobs
                     jobsInBunch = jobs[indexJob-iJob-1:indexJob-1]
                     if jobsInBunch != [] and fileList != [] and (not computingSite in prestageSites) \
                            and (jobsInBunch[0].prodSourceLabel in ['managed','software'] or \
                                 re.search('test',jobsInBunch[0].prodSourceLabel) != None):
                         # get site spec
                         tmp_chosen_ce = siteMapper.getSite(computingSite)
                         # get files from LRC 
                         okFiles = _getOkFiles(tmp_chosen_ce,fileList,guidList,allLFNs,allGUIDs,allOkFilesMap)
                         # loop over all jobs
                         for tmpJob in jobsInBunch:
                             # set 'ready' if files are already there
                             _setReadyToFiles(tmpJob,okFiles,siteMapper)
                else:
                    # load balancing
                    minSites = {}
                    nMinSites = 2
                    if prevBrokergageSiteList != []:
                        # special brokerage
                        scanSiteList = prevBrokergageSiteList
                    elif setScanSiteList == []:
                        if siteMapper.checkCloud(previousCloud):
                            # use cloud sites                    
                            scanSiteList = siteMapper.getCloud(previousCloud)['sites']
                        else:
                            # use default sites
                            scanSiteList = siteMapper.getCloud('default')['sites']
                    else:
                        # use given sites
                        scanSiteList = setScanSiteList
                        # add long queue
                        for tmpShortQueue,tmpLongQueue in shortLongMap.iteritems():
                            if tmpShortQueue in scanSiteList:
                                if not tmpLongQueue in scanSiteList:
                                    scanSiteList.append(tmpLongQueue)
                    # the number/size of inputs per job 
                    nFilesPerJob    = float(totalNumInputs)/float(iJob)
                    inputSizePerJob = float(totalInputSize)/float(iJob)
                    # use T1 for jobs with many inputs when weight is negative
                    if (not forAnalysis) and _isTooManyInput(nFilesPerJob,inputSizePerJob) and \
                           siteMapper.getCloud(previousCloud)['weight'] < 0:
                        scanSiteList = [siteMapper.getCloud(previousCloud)['source']]
                        # set site list to use T1 and T1_VL
                        if hospitalQueueMap.has_key(previousCloud):
                            scanSiteList += hospitalQueueMap[previousCloud]
                    # get availabe sites with cache
                    useCacheVersion = False
                    siteListWithCache = []
                    if forAnalysis:
                        if re.search('-\d+\.\d+\.\d+\.\d+',prevRelease) != None:
                            useCacheVersion = True
                            siteListWithCache = taskBuffer.checkSitesWithRelease(scanSiteList,caches=prevRelease,cmtConfig=prevCmtConfig)
                            tmpLog.debug('  using installSW for cache %s' % prevRelease)
                        elif re.search('-\d+\.\d+\.\d+$',prevRelease) != None:
                            useCacheVersion = True
                            siteListWithCache = taskBuffer.checkSitesWithRelease(scanSiteList,releases=prevRelease,cmtConfig=prevCmtConfig)
                            tmpLog.debug('  using installSW for release %s' % prevRelease)
                        elif re.search(':rel_\d+$$',prevRelease) != None:
                            useCacheVersion = True
                            iteListWithCache = taskBuffer.checkSitesWithRelease(scanSiteList,
                                                                                releases=prevRelease.split(':')[0],
                                                                                caches=prevRelease.split(':')[1],
                                                                                cmtConfig=prevCmtConfig)
                            tmpLog.debug('  using installSW for release:cache %s' % prevRelease)
                    elif previousCloud in ['DE','NL','FR','CA','ES','IT','TW','UK','US','ND','CERN']:
                            useCacheVersion = True
                            # change / to -
                            convedPrevHomePkg = prevHomePkg.replace('/','-')
                            if re.search('rel_\d+(\n|$)',prevHomePkg) == None:
                                # only cache is used for normal jobs
                                siteListWithCache = taskBuffer.checkSitesWithRelease(scanSiteList,caches=convedPrevHomePkg,
                                                                                     cmtConfig=prevCmtConfig)
                            else:
                                # both AtlasRelease and homepackage are used for nightlies
                                siteListWithCache = taskBuffer.checkSitesWithRelease(scanSiteList,
                                                                                     releases=prevRelease,
                                                                                     caches=convedPrevHomePkg,
                                                                                     cmtConfig=prevCmtConfig)
                            tmpLog.debug('  cache          %s' % prevHomePkg)
                    if useCacheVersion:        
                        tmpLog.debug('  cache/relSites     %s' % str(siteListWithCache))
                    # release/cmtconfig check
                    foundRelease   = False
                    # found candidate
                    foundOneCandidate = False
                    # randomize the order
                    if forAnalysis:
                        random.shuffle(scanSiteList)
                    # get cnadidates    
                    if True:
                        # loop over all sites    
                        for site in scanSiteList:
                            tmpLog.debug('calculate weight for site:%s' % site)
                            # _allSites may conain NULL after sort()
                            if site == 'NULL':
                                continue
                            # ignore test sites
                            if (prevManualPreset == False) and (site.endswith('test') or \
                                                                site.endswith('Test') or site.startswith('Test')):
                                continue
                            # ignore analysis queues
                            if (not forAnalysis) and site.startswith('ANALY'):
                                continue
                            # get SiteSpec
                            if siteMapper.checkSite(site):
                                tmpSiteSpec = siteMapper.getSite(site)
                            else:
                                tmpLog.debug(" skip: %s doesn't exist in DB" % site)
                                continue
                            # check status
                            if tmpSiteSpec.status in ['offline','brokeroff'] and computingSite in ['NULL',None,'']:
                                if forAnalysis and tmpSiteSpec.status == 'brokeroff' and tmpSiteSpec.accesscontrol == 'grouplist':
                                    # ignore brokeroff for grouplist site
                                    pass
                                elif forAnalysis and  prevProType in ['hammercloud','gangarobot','gangarobot-squid']:
                                    # ignore site status for HC
                                    pass
                                else:
                                    tmpLog.debug(' skip: status %s' % tmpSiteSpec.status)
                                    resultsForAnal['status'].append(site)                                    
                                    continue
                            if tmpSiteSpec.status == 'test' and (not prevProType in ['prod_test','hammercloud','gangarobot','gangarobot-squid']) \
                                   and not prevSourceLabel in ['test','prod_test']:
                                tmpLog.debug(' skip: status %s for %s' % (tmpSiteSpec.status,prevProType))
                                resultsForAnal['status'].append(site)
                                continue
                            tmpLog.debug('   status=%s' % tmpSiteSpec.status)
                            # check core count
                            if tmpSiteSpec.coreCount > 1:
                                # use multi-core queue for MP jobs
                                if not prevCoreCount in [None,'NULL'] and prevCoreCount > 1:
                                    pass
                                else:
                                    tmpLog.debug('  skip: MP site (%s core) for job.coreCount=%s' % (tmpSiteSpec.coreCount,
                                                                                                   prevCoreCount))
                                    resultsForAnal['cpucore'].append(site)
                                    continue
                            else:
                                # use single core for non-MP jobs
                                if not prevCoreCount in [None,'NULL'] and prevCoreCount > 1:
                                    tmpLog.debug('  skip: single core site (%s core) for job.coreCount=%s' % (tmpSiteSpec.coreCount,
                                                                                                            prevCoreCount))
                                    resultsForAnal['cpucore'].append(site)
                                    continue
                            # check memory
                            if tmpSiteSpec.memory != 0 and not prevMemory in [None,0,'NULL']:
                                try:
                                    if int(tmpSiteSpec.memory) < int(prevMemory):
                                        tmpLog.debug('  skip: memory shortage %s<%s' % (tmpSiteSpec.memory,prevMemory))
                                        resultsForAnal['memory'].append(site)
                                        continue
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("memory check : %s %s" % (errtype,errvalue))
                            # check maxcpucount
                            if tmpSiteSpec.maxtime != 0 and not prevMaxCpuCount in [None,0,'NULL']:
                                try:
                                    if int(tmpSiteSpec.maxtime) < int(prevMaxCpuCount):
                                        tmpLog.debug('  skip: insufficient maxtime %s<%s' % (tmpSiteSpec.maxtime,prevMaxCpuCount))
                                        resultsForAnal['maxtime'].append(site)
                                        continue
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("maxtime check : %s %s" % (errtype,errvalue))
                            # check max input size
                            if tmpSiteSpec.maxinputsize != 0 and (not prevDiskCount in [None,0,'NULL']):
                                try:
                                    if int(tmpSiteSpec.maxinputsize) < int(prevDiskCount):
                                        tmpLog.debug('  skip: not enough disk %s<%s' % (tmpSiteSpec.maxinputsize,prevDiskCount))
                                        resultsForAnal['scratch'].append(site)
                                        continue
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("disk check : %s %s" % (errtype,errvalue))
                            tmpLog.debug('   maxinput=%s' % tmpSiteSpec.maxinputsize)
                            # reliability
                            if forAnalysis and isinstance(siteReliability,types.IntType):
                                if tmpSiteSpec.reliabilityLevel != None and tmpSiteSpec.reliabilityLevel > siteReliability:
                                    tmpLog.debug(' skip: insufficient reliability %s > %s' % (tmpSiteSpec.reliabilityLevel,siteReliability))
                                    resultsForAnal['reliability'].append(site)
                                    continue
                            # change NULL cmtconfig to slc3/4
                            if prevCmtConfig in ['NULL','',None]:
                                if forAnalysis:
                                    tmpCmtConfig = 'i686-slc4-gcc34-opt'
                                else:
                                    tmpCmtConfig = 'i686-slc3-gcc323-opt'                                    
                            else:
                                tmpCmtConfig = prevCmtConfig
                            # set release
                            releases = tmpSiteSpec.releases
                            if prevProType in ['reprocessing']:
                                # use validated releases for reprocessing
                                releases = tmpSiteSpec.validatedreleases
                            if not useCacheVersion:    
                                tmpLog.debug('   %s' % str(releases))
                            if forAnalysis and (tmpSiteSpec.cloud in ['ND'] or prevRelease==''):
                                # doesn't check releases for analysis
                                tmpLog.debug(' no release check')
                                pass
                            elif forAnalysis and useCacheVersion:
                                # cache matching 
                                if not site in siteListWithCache:
                                    tmpLog.debug(' skip: cache %s/%s not found' % (prevRelease.replace('\n',' '),prevCmtConfig))
                                    if trustIS:
                                        resultsForAnal['rel'].append(site)
                                    continue
                            elif prevRelease != None and \
                                     (useCacheVersion and not tmpSiteSpec.cloud in ['ND'] and not site in ['CERN-RELEASE']) and \
                                     (not prevProType in ['reprocessing']) and \
                                     (not site in siteListWithCache):
                                    tmpLog.debug(' skip: cache %s/%s not found' % (prevHomePkg.replace('\n',' '),prevCmtConfig))
                                    # send message to logger
                                    try:
                                        if prevSourceLabel in ['managed','test']:
                                            resultsForAnal['rel'].append(site)                                    
                                            # make message
                                            message = '%s - cache %s/%s not found' % (site,prevHomePkg.replace('\n',' '),prevCmtConfig)
                                            if not message in loggerMessages:
                                                loggerMessages.append(message)
                                    except:
                                        pass
                                    continue
                            elif prevRelease != None and \
                                 ((not useCacheVersion and releases != [] and not tmpSiteSpec.cloud in ['ND'] and not site in ['CERN-RELEASE']) or prevProType in ['reprocessing']) and \
                                 (((not _checkRelease(prevRelease,releases) and prevManualPreset == False) or not site in siteListWithCache) and not tmpSiteSpec.cloud in ['ND'] and not site in ['CERN-RELEASE']):
                                # release matching
                                if not useCacheVersion:
                                    tmpLog.debug(' skip: release %s/%s not found' % (prevRelease.replace('\n',' '),prevCmtConfig))
                                else:
                                    tmpLog.debug(' skip: repro cache %s/%s not found' % (prevHomePkg.replace('\n',' '),prevCmtConfig))
                                resultsForAnal['rel'].append(site)
                                continue
                            elif not foundRelease:
                                # found at least one site has the release
                                foundRelease = True
                            # direct access
                            if prevDirectAcc == 'direct' and not tmpSiteSpec.allowdirectaccess:
                                tmpLog.debug(' skip: no direct access support')
                                continue
                            # get pilot statistics
                            nPilotsGet = 0
                            nPilotsUpdate = 0
                            if nWNmap == {}:
                                nWNmap = taskBuffer.getCurrentSiteData()
                            if nWNmap.has_key(site):    
                                nPilots = nWNmap[site]['getJob'] + nWNmap[site]['updateJob']
                                nPilotsGet = nWNmap[site]['getJob']
                                nPilotsUpdate = nWNmap[site]['updateJob']
                            else:
                                nPilots = 0
                            # if no pilots
                            if nPilots == 0 and nWNmap != {}:
                                tmpLog.debug(" skip: %s no pilot" % site)
                                resultsForAnal['pilot'].append(site)
                                continue
                            # if no jobs in jobsActive/jobsDefined
                            if not jobStatistics.has_key(site):
                                jobStatistics[site] = {'assigned':0,'activated':0,'running':0,'transferring':0}
                            # set nRunning 
                            if forAnalysis:
                                if not nRunningMap.has_key(site):
                                    nRunningMap[site] = 0
                            # check space
                            if specialWeight != {}:
                                # for PD2P
                                if sizeMapForCheck.has_key(site):
                                    # threshold for PD2P max(5%,3TB)
                                    thrForThisSite = long(sizeMapForCheck[site]['total'] * 5 / 100)
                                    if thrForThisSite < diskThresholdPD2P:
                                        thrForThisSite = diskThresholdPD2P
                                    remSpace = sizeMapForCheck[site]['total'] - sizeMapForCheck[site]['used']
                                    tmpLog.debug('   space available=%s remain=%s thr=%s' % (sizeMapForCheck[site]['total'],
                                                                                           remSpace,thrForThisSite))
                                    if remSpace-datasetSize < thrForThisSite:
                                        tmpLog.debug('  skip: disk shortage %s-%s< %s' % (remSpace,datasetSize,thrForThisSite))
                                        if getWeight:
                                            weightUsedByBrokerage[site] = "NA : disk shortage"
                                        continue
                            elif site != siteMapper.getCloud(previousCloud)['source']:
                                # for T2
                                if tmpSiteSpec.space != 0:
                                    nRemJobs = jobStatistics[site]['assigned']+jobStatistics[site]['activated']+jobStatistics[site]['running']
                                    if not forAnalysis:
                                        # take assigned/activated/running jobs into account for production
                                        remSpace = tmpSiteSpec.space - 0.250*nRemJobs
                                    else:
                                        remSpace = tmpSiteSpec.space
                                    tmpLog.debug('   space available=%s remain=%s' % (tmpSiteSpec.space,remSpace))
                                    if remSpace < diskThreshold:
                                        tmpLog.debug('  skip: disk shortage < %s' % diskThreshold)
                                        resultsForAnal['disk'].append(site)
                                        # keep message to logger
                                        try:
                                            if prevSourceLabel in ['managed','test']:
                                                # make message
                                                message = '%s - disk %s < %s' % (site,remSpace,diskThreshold)
                                                if not message in loggerMessages:
                                                    loggerMessages.append(message)
                                        except:
                                            pass
                                        continue
                            # get the process group
                            tmpProGroup = ProcessGroups.getProcessGroup(prevProType)
                            if prevProType in skipBrokerageProTypes:
                                # use original processingType since prod_test is in the test category and thus is interfered by validations 
                                tmpProGroup = prevProType
                            # production share
                            skipDueToShare = False
                            try:
                                if not forAnalysis and prevSourceLabel in ['managed'] and faresharePolicy.has_key(site):
                                    for tmpPolicy in faresharePolicy[site]['policyList']:
                                        # ignore priority policy
                                        if tmpPolicy['priority'] != None:
                                            continue
                                        # only zero share
                                        if tmpPolicy['share'] != '0%':
                                            continue
                                        # check group
                                        if tmpPolicy['group'] != None:
                                            if '*' in tmpPolicy['group']:
                                                # wildcard
                                                tmpPatt = '^' + tmpPolicy['group'].replace('*','.*') + '$'
                                                if re.search(tmpPatt,prevWorkingGroup) == None:
                                                    continue
                                            else:
                                                # normal definition
                                                if prevWorkingGroup != tmpPolicy['group']:
                                                    continue
                                        else:
                                            # catch all except WGs used by other policies
                                            groupInDefList = faresharePolicy[site]['groupList']
                                            usedByAnother = False
                                            # loop over all groups
                                            for groupInDefItem in groupInDefList:
                                                if '*' in groupInDefItem:
                                                    # wildcard
                                                    tmpPatt = '^' + groupInDefItem.replace('*','.*') + '$'
                                                    if re.search(tmpPatt,prevWorkingGroup) != None:
                                                        usedByAnother = True
                                                        break
                                                else:
                                                    # normal definition
                                                    if prevWorkingGroup == groupInDefItem:
                                                        usedByAnother = True
                                                        break
                                            if usedByAnother:
                                                continue
                                        # check type
                                        if tmpPolicy['type'] != None:
                                            if tmpPolicy['type'] == tmpProGroup:
                                                skipDueToShare = True
                                                break
                                        else:
                                            # catch all except PGs used by other policies
                                            typeInDefList  = faresharePolicy[site]['typeList'][tmpPolicy['group']]
                                            usedByAnother = False
                                            for typeInDefItem in typeInDefList:
                                                if typeInDefItem == tmpProGroup:
                                                    usedByAnother = True
                                                    break
                                            if not usedByAnother:    
                                                skipDueToShare = True
                                                break
                                    # skip        
                                    if skipDueToShare:    
                                        tmpLog.debug(" skip: %s zero share" % site)
                                        resultsForAnal['share'].append(site)
                                        continue
                            except:
                                errtype,errvalue = sys.exc_info()[:2]
                                tmpLog.error("share check : %s %s" % (errtype,errvalue))
                            # the number of assigned and activated
                            if not forAnalysis:                                
                                if not jobStatBrokerClouds.has_key(previousCloud):
                                    jobStatBrokerClouds[previousCloud] = {}
                                # use number of jobs in the cloud    
                                jobStatBroker = jobStatBrokerClouds[previousCloud]
                            if not jobStatBroker.has_key(site):
                                jobStatBroker[site] = {}
                            if not jobStatBroker[site].has_key(tmpProGroup):
                                jobStatBroker[site][tmpProGroup] = {'assigned':0,'activated':0,'running':0,'transferring':0}
                            # count # of assigned and activated jobs for prod by taking priorities in to account
                            nRunJobsPerGroup = None
                            if not forAnalysis and prevSourceLabel in ['managed','test']:
                                if not jobStatBrokerCloudsWithPrio.has_key(prevPriority):
                                    jobStatBrokerCloudsWithPrio[prevPriority] = taskBuffer.getJobStatisticsBrokerage(prevPriority)
                                if not jobStatBrokerCloudsWithPrio[prevPriority].has_key(previousCloud):
                                    jobStatBrokerCloudsWithPrio[prevPriority][previousCloud] = {}
                                if not jobStatBrokerCloudsWithPrio[prevPriority][previousCloud].has_key(site):
                                    jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site] = {}
                                if not jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site].has_key(tmpProGroup):
                                    jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site][tmpProGroup] = {'assigned':0,'activated':0,'running':0,'transferring':0}
                                nAssJobs = jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site][tmpProGroup]['assigned']
                                nActJobs = jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site][tmpProGroup]['activated']
                                nRunJobsPerGroup = jobStatBrokerCloudsWithPrio[prevPriority][previousCloud][site][tmpProGroup]['running']
                                # add newly assigned jobs
                                for tmpNewPriority in newJobStatWithPrio.keys():
                                    if tmpNewPriority < prevPriority:
                                        continue
                                    if not newJobStatWithPrio[tmpNewPriority].has_key(previousCloud):
                                        continue
                                    if not newJobStatWithPrio[tmpNewPriority][previousCloud].has_key(site):
                                        continue
                                    if not newJobStatWithPrio[tmpNewPriority][previousCloud][site].has_key(tmpProGroup):
                                        continue
                                    nAssJobs += newJobStatWithPrio[tmpNewPriority][previousCloud][site][tmpProGroup]
                            else:
                                nAssJobs = jobStatBroker[site][tmpProGroup]['assigned']
                                if forAnalysis and jobStatBroker[site][tmpProGroup].has_key('defined'):
                                    nAssJobs += jobStatBroker[site][tmpProGroup]['defined']
                                nActJobs = jobStatBroker[site][tmpProGroup]['activated']
                            # number of jobs per node
                            if not nWNmap.has_key(site):
                                nJobsPerNode = 1
                            elif jobStatistics[site]['running']==0 or nWNmap[site]['updateJob']==0:
                                nJobsPerNode = 1
                            else:
                                if nRunJobsPerGroup == None:
                                    nJobsPerNode = float(jobStatistics[site]['running'])/float(nWNmap[site]['updateJob'])
                                else:
                                    if nRunJobsPerGroup == 0:
                                        nJobsPerNode = 1.0/float(nWNmap[site]['updateJob'])
                                    else:
                                        nJobsPerNode = float(nRunJobsPerGroup)/float(nWNmap[site]['updateJob'])
                            # limit of the number of transferring jobs
                            if tmpSiteSpec.transferringlimit == 0:
                                maxTransferring   = 2000
                            else:
                                maxTransferring = tmpSiteSpec.transferringlimit
                            # get ration of transferring to running
                            if not forAnalysis and not tmpSiteSpec.cloud in ['ND']:
                                nTraJobs = 0
                                nRunJobs = 0
                                for tmpGroupForTra,tmpCountsForTra in jobStatBroker[site].iteritems():
                                    if tmpCountsForTra.has_key('running'):
                                        nRunJobs += tmpCountsForTra['running']
                                    if tmpCountsForTra.has_key('transferring'):
                                        nTraJobs += tmpCountsForTra['transferring']
                                tmpLog.debug('   running=%s transferring=%s max=%s' % (nRunJobs,nTraJobs,maxTransferring))
                                if max(maxTransferring,2*nRunJobs) < nTraJobs:
                                    tmpLog.debug(" skip: %s many transferring=%s > max(%s,2*running=%s)" % (site,nTraJobs,maxTransferring,nRunJobs))
                                    resultsForAnal['transferring'].append(site)
                                    if prevSourceLabel in ['managed','test']:
                                        # make message
                                        message = '%s - too many transferring' % site
                                        if not message in loggerMessages:
                                            loggerMessages.append(message)
                                    continue
                            # get ratio of running jobs = run(cloud)/run(all) for multi cloud
                            multiCloudFactor = 1
                            if not forAnalysis:                                
                                tmpTotalRunningMulti = 0
                                tmpNCloudMulti = 0
                                for tmpCloudMulti,tmpCloudValMulti in jobStatBrokerClouds.iteritems():
                                    if tmpCloudValMulti.has_key(site):
                                        if tmpCloudValMulti[site].has_key(tmpProGroup):
                                            tmpNCloudMulti += 1
                                            if tmpCloudValMulti[site][tmpProGroup].has_key('running'):
                                                tmpTotalRunningMulti += tmpCloudValMulti[site][tmpProGroup]['running']
                                # no running
                                if tmpTotalRunningMulti == 0:
                                    if tmpNCloudMulti != 0:
                                        multiCloudFactor = tmpNCloudMulti
                                else:
                                    multiCloudFactor = float(tmpTotalRunningMulti+1)/float(jobStatBroker[site][tmpProGroup]['running']+1)
                                tmpLog.debug('   totalRun:%s cloudRun:%s multiCloud:%s' % (tmpTotalRunningMulti,
                                                                                         jobStatBroker[site][tmpProGroup]['running'],
                                                                                         multiCloudFactor))
                            # country preference
                            preferredCountryWeight = 1.0
                            preferredCountryWeightStr = ''
                            if forAnalysis:
                                if preferredCountries != [] and tmpSiteSpec.countryGroup != []:
                                    for tmpCountry in preferredCountries:
                                        if tmpCountry in tmpSiteSpec.countryGroup:
                                            # avoid negative weight or zero-divide
                                            if tmpSiteSpec.availableCPU >= tmpSiteSpec.pledgedCPU and tmpSiteSpec.pledgedCPU > 0:
                                                preferredCountryWeight = float(tmpSiteSpec.availableCPU) / float(tmpSiteSpec.pledgedCPU)
                                                preferredCountryWeightStr = "*(%s/%s)" % (tmpSiteSpec.availableCPU,tmpSiteSpec.pledgedCPU)
                                                resultsForAnal['prefcountry'].append((site,tmpCountry))
                                            break
                                tmpLog.debug('   country preference=%s' % preferredCountryWeightStr[1:])
                            # calculate weight
                            if specialWeight != {}:
                                if not pd2pT1:
                                    # weight for T2 PD2P
                                    nSubs = 1
                                    if specialWeight.has_key(site):
                                        nSubs = specialWeight[site]
                                    tmpLog.debug('   %s nSubs:%s assigned:%s activated:%s running:%s nWNsG:%s nWNsU:%s' % \
                                               (site,nSubs,nAssJobs,nActJobs,nRunningMap[site],nPilotsGet,nPilotsUpdate))
                                    winv = float(nSubs) * float(nAssJobs+nActJobs) / float(1+nRunningMap[site]) / (1.0+float(nPilotsGet)/float(1+nPilotsUpdate))
                                    if getWeight:
                                        weightUsedByBrokerage[site] = "(1+%s/%s)*%s/%s/%s" % (nPilotsGet,1+nPilotsUpdate,1+nRunningMap[site],nAssJobs+nActJobs,nSubs)
                                else:
                                    # weight for T1 PD2P
                                    tmpLog.debug('   %s MoU:%s' % (site,specialWeight[site]))
                                    winv = 1.0 / float(specialWeight[site])
                                    if getWeight:
                                        weightUsedByBrokerage[site] = "%s" % specialWeight[site]
                            else:
                                if not forAnalysis:
                                    if nRunJobsPerGroup == None:
                                        tmpLog.debug('   %s assigned:%s activated:%s running:%s nPilots:%s nJobsPerNode:%s multiCloud:%s' %
                                                     (site,nAssJobs,nActJobs,jobStatistics[site]['running'],nPilots,nJobsPerNode,multiCloudFactor))
                                    else:
                                        tmpLog.debug('   %s assigned:%s activated:%s running:%s nPilots:%s nJobsPerNodePG:%s multiCloud:%s' %
                                                     (site,nAssJobs,nActJobs,nRunJobsPerGroup,nPilots,nJobsPerNode,multiCloudFactor))
                                else:
                                    tmpLog.debug('   %s assigned:%s activated:%s running:%s nWNsG:%s nWNsU:%s' %
                                               (site,nAssJobs,nActJobs,nRunningMap[site],nPilotsGet,nPilotsUpdate))
                                if forAnalysis:
                                    winv = float(nAssJobs+nActJobs) / float(1+nRunningMap[site]) / (1.0+float(nPilotsGet)/float(1+nPilotsUpdate))
                                elif nPilots != 0:
                                    winv = (float(nAssJobs+nActJobs)) / float(nPilots) / nJobsPerNode
                                else:
                                    winv = (float(nAssJobs+nActJobs)) / nJobsPerNode
                                winv *= float(multiCloudFactor)    
                                # send jobs to T1 when they require many or large inputs
                                if _isTooManyInput(nFilesPerJob,inputSizePerJob):
                                    if site == siteMapper.getCloud(previousCloud)['source'] or \
                                       (site=='NIKHEF-ELPROD' and previousCloud=='NL' and prevProType=='reprocessing') or \
                                       (hospitalQueueMap.has_key(previousCloud) and site in hospitalQueueMap[previousCloud]):
                                        cloudT1Weight = 2.0
                                        # use weight in cloudconfig
                                        try:
                                            tmpCloudT1Weight = float(siteMapper.getCloud(previousCloud)['weight'])
                                            if tmpCloudT1Weight != 0.0:
                                                cloudT1Weight = tmpCloudT1Weight
                                        except:
                                            pass
                                        winv /= cloudT1Weight
                                        tmpLog.debug('   special weight for %s : nInputs/Job=%s inputSize/Job=%s weight=%s' % 
                                                   (site,nFilesPerJob,inputSizePerJob,cloudT1Weight))
                            # found at least one candidate
                            foundOneCandidate = True
                            tmpLog.debug('Site:%s 1/Weight:%s' % (site,winv))
                            if forAnalysis and trustIS and reportLog:
                                resultsForAnal['weight'].append((site,'(1+%s/%s)*%s/%s%s' % (nPilotsGet,1+nPilotsUpdate,1+nRunningMap[site],
                                                                                             nAssJobs+nActJobs,preferredCountryWeightStr)))
                            # choose largest nMinSites weights
                            minSites[site] = winv
                            if len(minSites) > nMinSites:
                                maxSite = site
                                maxWinv = winv
                                for tmpSite,tmpWinv in minSites.iteritems():
                                    if tmpWinv > maxWinv:
                                        maxSite = tmpSite
                                        maxWinv = tmpWinv
                                # delte max one
                                del minSites[maxSite]
                            # remove too different weights
                            if len(minSites) >= 2:
                                # look for minimum
                                minSite = minSites.keys()[0]
                                minWinv = minSites[minSite]
                                for tmpSite,tmpWinv in minSites.iteritems():
                                    if tmpWinv < minWinv:
                                        minSite = tmpSite
                                        minWinv = tmpWinv
                                # look for too different weights
                                difference = 2
                                removeSites = []
                                for tmpSite,tmpWinv in minSites.iteritems():
                                    if tmpWinv > minWinv*difference:
                                        removeSites.append(tmpSite)
                                # remove
                                for tmpSite in removeSites:
                                    del minSites[tmpSite]
                    # set default
                    if len(minSites) == 0:
                        # cloud's list
                        if forAnalysis or siteMapper.checkCloud(previousCloud):
                            minSites[scanSiteList[0]] = 0
                        else:
                            minSites['BNL_ATLAS_1'] = 0
                        # release not found
                        if forAnalysis and trustIS:
                            candidateForAnal = False
                    # use only one site for prod_test to skip LFC scan
                    if prevProType in skipBrokerageProTypes:
                        if len(minSites) > 1:
                            minSites = {minSites.keys()[0]:0}
                    # choose site
                    tmpLog.debug('Min Sites:%s' % minSites)
                    if len(fileList) ==0:
                        # choose min 1/weight
                        minSite = minSites.keys()[0]
                        minWinv = minSites[minSite]
                        for tmpSite,tmpWinv in minSites.iteritems():
                            if tmpWinv < minWinv:
                                minSite = tmpSite
                                minWinv = tmpWinv
                        chosenCE = siteMapper.getSite(minSite)
                    else:
                        # compare # of files in LRC
                        maxNfiles = -1
                        for site in minSites:
                            tmp_chosen_ce = siteMapper.getSite(site)
                            # search LRC
                            if site in _disableLRCcheck:
                                tmpOKFiles = {}
                            else:
                                # get files from LRC 
                                tmpOKFiles = _getOkFiles(tmp_chosen_ce,fileList,guidList,allLFNs,allGUIDs,allOkFilesMap)
                            nFiles = len(tmpOKFiles)
                            tmpLog.debug('site:%s - nFiles:%s' % (site,nFiles))
                            # choose site holding max # of files
                            if nFiles > maxNfiles:
                                chosenCE = tmp_chosen_ce
                                maxNfiles = nFiles
                                okFiles = tmpOKFiles
                    # set job spec
                    tmpLog.debug('indexJob      : %s' % indexJob)
                    tmpLog.debug('nInputs/Job   : %s' % nFilesPerJob)
                    tmpLog.debug('inputSize/Job : %s' % inputSizePerJob)
                    for tmpJob in jobs[indexJob-iJob-1:indexJob-1]:
                        # set computingSite
                        if (not candidateForAnal) and forAnalysis and trustIS:
                            resultsForAnalStr = 'ERROR : No candidate. '
                            if resultsForAnal['rel'] != []:
                                if prevCmtConfig in ['','NULL',None]:
                                    resultsForAnalStr += 'Release:%s was not found at %s. ' % (prevRelease,str(resultsForAnal['rel']))
                                else:
                                    resultsForAnalStr += 'Release:%s/%s was not found at %s. ' % (prevRelease,prevCmtConfig,str(resultsForAnal['rel']))
                            if resultsForAnal['pilot'] != []:
                                resultsForAnalStr += '%s are inactive (no pilots for last 3 hours). ' % str(resultsForAnal['pilot'])
                            if resultsForAnal['disk'] != []:
                                resultsForAnalStr += 'Disk shortage < %sGB at %s. ' % (diskThreshold,str(resultsForAnal['disk']))
                            if resultsForAnal['memory'] != []:
                                resultsForAnalStr += 'Insufficient RAM at %s. ' % str(resultsForAnal['memory'])
                            if resultsForAnal['maxtime'] != []:
                                resultsForAnalStr += 'Shorter walltime limit than maxCpuCount:%s at ' % prevMaxCpuCount
                                for tmpItem in resultsForAnal['maxtime']:
                                    if siteMapper.checkSite(tmpItem):
                                        resultsForAnalStr += '%s:%s,' % (tmpItem,siteMapper.getSite(tmpItem).maxtime)
                                resultsForAnalStr = resultsForAnalStr[:-1]        
                                resultsForAnalStr += '. '
                            if resultsForAnal['status'] != []:
                                resultsForAnalStr += '%s are not online. ' % str(resultsForAnal['status'])
                            if resultsForAnal['reliability'] != []:
                                resultsForAnalStr += 'Insufficient reliability at %s. ' % str(resultsForAnal['reliability'])
                            resultsForAnalStr = resultsForAnalStr[:-1]
                            tmpJob.computingSite = resultsForAnalStr
                        else:
                            tmpJob.computingSite = chosenCE.sitename
                        # send log
                        if forAnalysis and trustIS and reportLog:
                            # put logging info to ErrorDiag just to give it back to the caller
                            tmpJob.brokerageErrorDiag = sendAnalyBrokeageInfo(resultsForAnal,prevRelease,diskThreshold,
                                                                              tmpJob.computingSite,prevCmtConfig,
                                                                              siteReliability)
                        tmpLog.debug('PandaID:%s -> site:%s' % (tmpJob.PandaID,tmpJob.computingSite))
                        if tmpJob.computingElement == 'NULL':
                            if tmpJob.prodSourceLabel == 'ddm':
                                # use nickname for ddm jobs
                                tmpJob.computingElement = chosenCE.nickname
                            else:
                                tmpJob.computingElement = chosenCE.gatekeeper
                        # fail jobs if no sites have the release
                        if (not foundRelease or (tmpJob.relocationFlag != 1 and not foundOneCandidate)) and (tmpJob.prodSourceLabel in ['managed','test']):
                            # reset
                            if tmpJob.relocationFlag != 1:
                                tmpJob.computingSite = None
                                tmpJob.computingElement = None
                            # go to waiting
                            tmpJob.jobStatus          = 'waiting'
                            tmpJob.brokerageErrorCode = ErrorCode.EC_Release
                            if tmpJob.relocationFlag == 1:
                                try:
                                    if resultsForAnal['pilot'] != []:
                                        tmpJob.brokerageErrorDiag = '%s no pilots' % tmpJob.computingSite
                                    elif resultsForAnal['disk'] != []:
                                        tmpJob.brokerageErrorDiag = 'SE full at %s' % tmpJob.computingSite
                                    elif resultsForAnal['memory'] != []:
                                        tmpJob.brokerageErrorDiag = 'RAM shortage at %s' % tmpJob.computingSite
                                    elif resultsForAnal['status'] != []:
                                        tmpJob.brokerageErrorDiag = '%s not online' % tmpJob.computingSite
                                    elif resultsForAnal['share'] != []:
                                        tmpJob.brokerageErrorDiag = '%s zero share' % tmpJob.computingSite
                                    elif resultsForAnal['cpucore'] != []:
                                        tmpJob.brokerageErrorDiag = "CPU core mismatch at %s" % tmpJob.computingSite
                                    elif resultsForAnal['maxtime'] != []:
                                        tmpJob.brokerageErrorDiag = "short walltime at %s" % tmpJob.computingSite
                                    elif resultsForAnal['transferring'] != []:
                                        tmpJob.brokerageErrorDiag = 'too many transferring at %s' % tmpJob.computingSite
                                    elif resultsForAnal['scratch'] != []:
                                        tmpJob.brokerageErrorDiag = 'small scratch disk at %s' % tmpJob.computingSite
                                    elif useCacheVersion:
                                        tmpJob.brokerageErrorDiag = '%s/%s not found at %s' % (tmpJob.homepackage,tmpJob.cmtConfig,tmpJob.computingSite)
                                    else:
                                        tmpJob.brokerageErrorDiag = '%s/%s not found at %s' % (tmpJob.AtlasRelease,tmpJob.cmtConfig,tmpJob.computingSite)
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("failed to set diag for %s: %s %s" % (tmpJob.PandaID,errtype,errvalue))
                                    tmpJob.brokerageErrorDiag = 'failed to set diag. see brokerage log in the panda server'
                            elif not prevBrokergageSiteList in [[],None]:
                                try:
                                    # make message
                                    tmpJob.brokerageErrorDiag = makeCompactDiagMessage(prevBrokerageNote,resultsForAnal)
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("failed to set special diag for %s: %s %s" % (tmpJob.PandaID,errtype,errvalue))
                                    tmpJob.brokerageErrorDiag = 'failed to set diag. see brokerage log in the panda server'
                            elif prevProType in ['reprocessing']:
                                tmpJob.brokerageErrorDiag = '%s/%s not found at reprocessing sites' % (tmpJob.homepackage,tmpJob.cmtConfig)
                            elif not useCacheVersion:
                                tmpJob.brokerageErrorDiag = '%s/%s not found at online sites with enough memory and disk' % \
                                                            (tmpJob.AtlasRelease,tmpJob.cmtConfig)
                            else:
                                try:
                                    tmpJob.brokerageErrorDiag = makeCompactDiagMessage('',resultsForAnal)
                                except:
                                    errtype,errvalue = sys.exc_info()[:2]
                                    tmpLog.error("failed to set compact diag for %s: %s %s" % (tmpJob.PandaID,errtype,errvalue))
                                    tmpJob.brokerageErrorDiag = 'failed to set diag. see brokerage log in the panda server'
                            tmpLog.debug('PandaID:%s %s' % (tmpJob.PandaID,tmpJob.brokerageErrorDiag))
                            continue
                        # set ready if files are already there
                        _setReadyToFiles(tmpJob,okFiles,siteMapper)                        
                        # update statistics
                        tmpProGroup = ProcessGroups.getProcessGroup(tmpJob.processingType)
                        if tmpJob.processingType in skipBrokerageProTypes:
                            # use original processingType since prod_test is in the test category and thus is interfered by validations 
                            tmpProGroup = tmpJob.processingType
                        if not jobStatistics.has_key(tmpJob.computingSite):
                            jobStatistics[tmpJob.computingSite] = {'assigned':0,'activated':0,'running':0}
                        if not jobStatBroker.has_key(tmpJob.computingSite):
                            jobStatBroker[tmpJob.computingSite] = {}
                        if not jobStatBroker[tmpJob.computingSite].has_key(tmpProGroup):
                            jobStatBroker[tmpJob.computingSite][tmpProGroup] = {'assigned':0,'activated':0,'running':0}
                        jobStatistics[tmpJob.computingSite]['assigned'] += 1
                        jobStatBroker[tmpJob.computingSite][tmpProGroup]['assigned'] += 1
                        # update statistics by taking priorities into account
                        if not forAnalysis and prevSourceLabel in ['managed','test']:
                            if not newJobStatWithPrio.has_key(prevPriority):
                                newJobStatWithPrio[prevPriority] = {}
                            if not newJobStatWithPrio[prevPriority].has_key(tmpJob.cloud):
                                newJobStatWithPrio[prevPriority][tmpJob.cloud] = {}
                            if not newJobStatWithPrio[prevPriority][tmpJob.cloud].has_key(tmpJob.computingSite):
                                newJobStatWithPrio[prevPriority][tmpJob.cloud][tmpJob.computingSite] = {}
                            if not newJobStatWithPrio[prevPriority][tmpJob.cloud][tmpJob.computingSite].has_key(tmpProGroup):
                                newJobStatWithPrio[prevPriority][tmpJob.cloud][tmpJob.computingSite][tmpProGroup] = 0
                            newJobStatWithPrio[prevPriority][tmpJob.cloud][tmpJob.computingSite][tmpProGroup] += 1
                # terminate
                if job == None:
                    break
                # reset iJob
                iJob = 0
                # reset file list
                fileList = []
                guidList = []            
                okFiles  = {}
                totalNumInputs = 0
                totalInputSize = 0
                # create new dispDBlock
                if job.prodDBlock != 'NULL':
                    # get datatype
                    try:
                        tmpDataType = job.prodDBlock.split('.')[-2]
                    except:
                        # default
                        tmpDataType = 'GEN'                        
                    if len(tmpDataType) > 20:
                        # avoid too long name
                        tmpDataType = 'GEN'
                    dispatchDBlock = "panda.%s.%s.%s.%s_dis%s" % (job.taskID,time.strftime('%m.%d'),tmpDataType,
                                                                  commands.getoutput('uuidgen'),job.PandaID)
                    tmpLog.debug('New dispatchDBlock: %s' % dispatchDBlock)                    
                prodDBlock = job.prodDBlock
                # already define computingSite
                if job.computingSite != 'NULL':
                    # instantiate KnownSite
                    chosen_ce = siteMapper.getSite(job.computingSite)
                    # if site doesn't exist, use ANALY_BNL_ATLAS_1
                    if job.homepackage.startswith('AnalysisTransforms'):
                        if chosen_ce.sitename == 'BNL_ATLAS_1':
                            chosen_ce = siteMapper.getSite('ANALY_BNL_ATLAS_1')
                            overwriteSite = True
                else:
                    # default for Analysis jobs
                    if job.homepackage.startswith('AnalysisTransforms'):
                        chosen_ce = siteMapper.getSite('ANALY_BNL_ATLAS_1')
                        overwriteSite = True                        
                    else:
                        # set chosen_ce
                        chosen_ce = 'TOBEDONE'
            # increment iJob
            iJob += 1
            # reserve computingSite and cloud
            computingSite   = job.computingSite
            previousCloud   = job.cloud
            prevRelease     = job.AtlasRelease
            prevMemory      = job.minRamCount
            prevCmtConfig   = job.cmtConfig
            prevProType     = job.processingType
            prevSourceLabel = job.prodSourceLabel
            prevDiskCount   = job.maxDiskCount
            prevHomePkg     = job.homepackage
            prevDirectAcc   = job.transferType
            prevCoreCount   = job.coreCount
            prevMaxCpuCount = job.maxCpuCount
            prevBrokergageSiteList = specialBrokergageSiteList
            prevManualPreset = manualPreset
            prevGoToT2Flag   = goToT2Flag
            prevWorkingGroup = job.workingGroup
            prevBrokerageNote = brokerageNote
            # truncate prio to avoid too many lookups
            if not job.currentPriority in [None,'NULL']:
                prevPriority = (job.currentPriority / 50) * 50
            # assign site
            if chosen_ce != 'TOBEDONE':
                job.computingSite = chosen_ce.sitename
                if job.computingElement == 'NULL':
                    if job.prodSourceLabel == 'ddm':
                        # use nickname for ddm jobs
                        job.computingElement = chosen_ce.nickname
                    else:
                        job.computingElement = chosen_ce.gatekeeper
                # update statistics
                if not jobStatistics.has_key(job.computingSite):
                    jobStatistics[job.computingSite] = {'assigned':0,'activated':0,'running':0}
                jobStatistics[job.computingSite]['assigned'] += 1
                tmpLog.debug('PandaID:%s -> preset site:%s' % (job.PandaID,chosen_ce.sitename))
                # set cloud
                if job.cloud in ['NULL',None,'']:
                    job.cloud = chosen_ce.cloud
            # set destinationSE
            destSE = job.destinationSE
            if siteMapper.checkCloud(job.cloud):
                # use cloud dest for non-exsiting sites
                if job.prodSourceLabel != 'user' and (not job.destinationSE in siteMapper.siteSpecList.keys()) \
                       and job.destinationSE != 'local':
                    destSE = siteMapper.getCloud(job.cloud)['dest'] 
                    job.destinationSE = destSE
            # use CERN-PROD_EOSDATADISK for CERN-EOS jobs
            if job.computingSite in ['CERN-EOS']:
                overwriteSite = True
            if overwriteSite:
                # overwrite SE for analysis jobs which set non-existing sites
                destSE = job.computingSite
                job.destinationSE = destSE
            # set dispatchDBlock and destinationSE
            first = True
            for file in job.Files:
                # dispatchDBlock. Set dispDB for prestaging jobs too
                if file.type == 'input' and file.dispatchDBlock == 'NULL' and \
                   ((not file.status in ['ready','missing']) or job.computingSite in prestageSites):
                    if first:
                        first = False
                        job.dispatchDBlock = dispatchDBlock
                    file.dispatchDBlock = dispatchDBlock
                    file.status = 'pending'
                    if not file.lfn in fileList:
                        fileList.append(file.lfn)
                        guidList.append(file.GUID)
                        try:
                            # get total number/size of inputs except DBRelease
                            # tgz inputs for evgen may be negligible
                            if re.search('\.tar\.gz',file.lfn) == None:
                                totalNumInputs += 1
                                totalInputSize += file.fsize
                        except:
                            pass
                # destinationSE
                if file.type in ['output','log'] and destSE != '':
                    if job.prodSourceLabel == 'user' and job.computingSite == file.destinationSE:
                        pass
                    elif destSE == 'local':
                        pass
                    else:
                        file.destinationSE = destSE
                # pre-assign GUID to log
                if file.type == 'log':
                    # get lock
                    fcntl.flock(_lockGetUU.fileno(), fcntl.LOCK_EX)                
                    # generate GUID
                    file.GUID = commands.getoutput('uuidgen')
                    # release lock
                    fcntl.flock(_lockGetUU.fileno(), fcntl.LOCK_UN)
        # send log messages
        try:
            for  message in loggerMessages:
                # get logger
                _pandaLogger = PandaLogger()
                _pandaLogger.lock()
                _pandaLogger.setParams({'Type':'brokerage'})
                logger = _pandaLogger.getHttpLogger(panda_config.loggername)
                # add message
                logger.warning(message)
                # release HTTP handler
                _pandaLogger.release()
                time.sleep(1)
        except:
            pass
        # send analysis brokerage info when jobs are submitted
        if len(jobs) > 0 and jobs[0] != None and not forAnalysis and not pd2pT1 and specialWeight=={}:
            # for analysis job. FIXME once ganga is updated to send analy brokerage info
            if jobs[0].prodSourceLabel in ['user','panda'] and jobs[0].processingType in ['pathena','prun']:
                # send countryGroup
                tmpMsgList = []
                tmpNumJobs = len(jobs)
                if jobs[0].prodSourceLabel == 'panda':
                    tmpNumJobs -= 1
                tmpMsg = 'nJobs=%s ' % tmpNumJobs
                if jobs[0].countryGroup in ['NULL','',None]:
                    tmpMsg += 'countryGroup=None'
                else:
                    tmpMsg += 'countryGroup=%s' % jobs[0].countryGroup
                tmpMsgList.append(tmpMsg)
                # send log
                sendMsgToLoggerHTTP(tmpMsgList,jobs[0])
        # finished            
        tmpLog.debug('finished')
        if getWeight:
            return weightUsedByBrokerage
    except:
        type, value, traceBack = sys.exc_info()
        tmpLog.error("schedule : %s %s" % (type,value))
        if getWeight:
            return {}

