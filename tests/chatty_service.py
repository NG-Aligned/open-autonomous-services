def run(ctx):
    for _ in range(20000):
        print("x" * 100)          # 2 MB to stdout, which the isolation layer redirects to stderr
    ctx.write("out", 1)
