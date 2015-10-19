__author__ = 'aerospike'


from lib.util import *

#print shell_command(['ls', '/Users/aerospike/work/code/'])

str = "migrates in progress"
str1 = "xyz"
file = '/Users/aerospike/work/vagrantShared/as_log_1444647618.61/aerospike2.log'
file1 = "/Users/aerospike/work/code/tst2.log"
latencyPattern1 = '%s (\d+)'
latencyPattern2 = '%s \(([0-9,\s]+)\)'
def grep(str, file):
    out, err = shell_command(['grep','\"'+str+'\"', file])
    return out


def grepCount(str, file):
    out, err = shell_command(['grep', '-o', '\"'+str+'\"', file, '|' 'wc -l'])
    return out

def grepDiff(str, file):
    result = []
    lines = grep(str, file).strip().split('\n')
    line = lines.pop(0)
    m1 = re.search( latencyPattern1%(str), line )
    m2 = re.search( latencyPattern2%(str), line )
    while(not m1 and not m2 and len(lines)>0):
        print line
        line = lines.pop(0)
        m1 = re.search( latencyPattern1%(str), line )
        print latencyPattern2%(str)
        m2 = re.search( latencyPattern2%(str), line )
        if m2:
            print "done"

    pattern = ""
    prev = []
    if(m1):
        pattern = latencyPattern1%(str)
        prev.append(int(m1.group(1)))
    elif(m2):
        pattern = latencyPattern2%(str)
        prev = map(lambda x: int(x), m2.group(1).split(","))

    for line in lines:
        #print line
        m = re.search( pattern, line )
        if(m):
            current = map(lambda x: int(x),m.group(1).split(","))
            result.append([b-a for b,a in zip(current,prev)])
            prev = current

    return result


print grepDiff(str, file1)

aa = [5,6,9]
bb = [7,3,2]

dd = [b-a for b,a in zip(bb,aa)]
#print dd

cc = ['12', '32', '35']
dd = map(lambda x: int(x),cc)
#print dd
strrr = "migrates in progress"
latencyPatter = '%s \(([0-9,\s]+)\)'%(strrr)
s = "Oct 12 2015 09:58:53 GMT: INFO (info): (thr_info.c::4844)  migrates in progress ( 2 , 0 ) ::: ClusterSize 3 abcd ::: objects 1605059 ::: sub_objects 0"
ss = "/var/log/aerospike/asla/as_log_1444289465.19/node0/aerospike.log:Oct 02 2015 10:32:30 GMT: INFO (info): (thr_info.c::4844)  migrates in progress ( 0 , 0 ) ::: ClusterSize 1 ::: objects 0 ::: sub_objects 0"
mmm = re.search( latencyPatter, ss)
print latencyPatter
if(mmm):
    print "yessss"


def stripString(search_str):
    print search_str
    print search_str[5]
    if(search_str[0]=="\"" or search_str[0]=="\'"):
        return search_str[1:len(search_str)-1]
    else:
        return search_str

str2 = "\"abcd\""
print str2
print stripString(str2)

