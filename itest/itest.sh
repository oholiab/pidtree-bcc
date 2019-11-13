#!/bin/bash -eE

export FIFO_NAME=itest/itest_output_$$
export TEST_SERVER_FIFO_NAME=itest/itest_server_$$
export TEST_PORT=${TEST_PORT:-31337}
export DEBUG=${DEBUG:-false}
export CONTAINER_NAME=pidtree-itest_$$

# The container takes a while to bootstrap so we have to wait before we emit the test event
SPIN_UP_TIME=40
# We also need to timout the test if the test event *isn't* caught
TIMEOUT=$(( SPIN_UP_TIME + 10 ))

function is_port_used {
  USED_PORTS=$(ss -4lnt | awk 'FS="[[:space:]]+" { print $4 }' | cut -d: -f2 | sort)
  if [ "$(echo "$USED_PORTS" | grep -E "^${TEST_PORT}\$")" = "$TEST_PORT" ]; then
    echo "ERROR: TEST_PORT=$TEST_PORT already in use, please reassign and try again"
    exit 2
  fi
}

function create_event {
  echo "Creating test listener"
  mkfifo $TEST_SERVER_FIFO_NAME
  cat $TEST_SERVER_FIFO_NAME | nc -l -p $TEST_PORT & 
  echo "Sleeping $SPIN_UP_TIME for pidtree-bcc to start"
  sleep $SPIN_UP_TIME
  echo "Making test connection"
  nc 127.1.33.7 $TEST_PORT & > /dev/null
  CLIENT_PID=$!
  sleep 3s
  echo "lolz" > $TEST_SERVER_FIFO_NAME
  echo "Killing test connection"
  kill $CLIENT_PID
}

function cleanup {
  echo "CLEANUP: Caught EXIT"
  set +eE
  echo "CLEANUP: Killing container"
  docker kill $CONTAINER_NAME
  echo "CLEANUP: Removing FIFO"
  rm -f $FIFO_NAME
}

function wait_for_tame_output {
  RESULTS=0
  echo "Tailing output FIFO $FIFO_NAME to catch test traffic"
  while read line; do
    RESULTS="$(echo "$line" | jq -r ". | select( .daddr == \"127.1.33.7\" ) | select( .port == $TEST_PORT) | .proctree[0].cmdline" 2>&1)"
    if [ "$RESULTS" = "nc 127.1.33.7 $TEST_PORT" ]; then
      echo "Caught test traffic on 127.1.33.7:$TEST_PORT!"
      return 0
    elif [ "$DEBUG" = "true" ]; then
      echo "DEBUG: \$RESULTS is $RESULTS"
      echo "DEBUG: \$line is $line"
    fi
  done < "$FIFO_NAME"
}

function main {
  is_port_used
  if [ "$DEBUG" = "true" ]; then set -x; fi
  echo "Building itest image"
  docker build -t pidtree-itest .
  mkfifo $FIFO_NAME
  echo "Launching itest-container $CONTAINER_NAME"
  docker run --name $CONTAINER_NAME -d\
      --rm --privileged --cap-add sys_admin --pid host \
      -v $(git rev-parse --show-toplevel)/itest/example_config.yml:/work/config.yml \
      -v $(git rev-parse --show-toplevel)/$FIFO_NAME:/work/outfile \
      pidtree-itest -c /work/config.yml -f /work/outfile
  export -f wait_for_tame_output
  export -f cleanup
  timeout $TIMEOUT bash -c wait_for_tame_output &
  WAIT_FOR_OUTPUT_PID=$!
  create_event &
  set +e
  wait $WAIT_FOR_OUTPUT_PID
  if [ $? -ne 0 ]; then
    echo "FAILED! (timeout)"
    exit 1
  else
    echo "SUCCESS!"
    exit 0
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  trap cleanup EXIT
  main "$@"
fi
