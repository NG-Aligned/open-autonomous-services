def run(ctx):
    # hostile/buggy child: pipelines RPCs without ever reading the replies, so the parent's sends back up
    chan = ctx._chan
    for _ in range(100000):
        chan.sock.sendall(b'{"call":"read","args":["big"],"kwargs":{}}\n')
