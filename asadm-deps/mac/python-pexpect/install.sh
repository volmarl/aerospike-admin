#!/usr/bin/env bash
################################################################################

PYMODULE=pexpect

################################################################################

command_exists () {
    type "$1" &> /dev/null ;
}

################################################################################

if [ $EUID -ne 0 ]; then
	echo "This script requires root or sudo privileges."
	exit 1
fi

python <<EOF
try:
	import ${PYMODULE}
	print "pexpect already installed on this machine"
	import sys
	sys.exit(0)
except Exception as e:
	import sys
	sys.exit(1)
EOF
has_pymodule=$?

if [ $has_pymodule -eq 0 ]; then
	exit 0
fi

if ! command_exists pip ; then
    echo Installing pip
	easy_install pip

	if ! command_exists pip ; then
		echo "Error while installing pip. Please install pip and run this installation again."
		exit 1
	fi
fi
echo Installing ${PYMODULE}
pip install ${PYMODULE}