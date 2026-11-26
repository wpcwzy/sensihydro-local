"""A timed-out injection must not leak its late answer to the vendor."""
import json, os, socket, subprocess, sys, threading, time, urllib.request

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/sensihydro-local"
received_by_vendor = bytearray()

def vendor(stop):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 15997)); srv.listen(5); srv.settimeout(0.5)
    conns = []
    while not stop.is_set():
        try:
            c, _ = srv.accept(); conns.append(c)
        except socket.timeout:
            pass
        for c in list(conns):
            try:
                c.settimeout(0.2); received_by_vendor.extend(c.recv(4096))
            except socket.timeout:
                pass
            except OSError:
                conns.remove(c)

def dtu(stop):
    time.sleep(1.5)
    c = socket.create_connection(("127.0.0.1", 15002), timeout=5)
    c.sendall(b"0034EAE724862A")
    c.settimeout(0.5); buf = b""; pending_late = []
    while not stop.is_set():
        try:
            data = c.recv(4096)
        except socket.timeout:
            for tid, due in list(pending_late):
                if time.time() >= due:
                    body = bytes([1, 3, 4, 0x03, 0xE8, 0x03, 0xE9])
                    c.sendall(tid + b"\x00\x00" + len(body).to_bytes(2, "big") + body)
                    pending_late.remove((tid, due))
            try: c.sendall(b"0034EAE724862A")
            except OSError: break
            continue
        except OSError:
            break
        if not data: break
        buf += data
        while len(buf) >= 6:
            ln = (buf[4] << 8) | buf[5]
            if len(buf) < 6 + ln: break
            f, buf = buf[:6+ln], buf[6+ln:]
            tid, pdu = f[:2], f[6:]
            # answer 7s later, i.e. after the bridge's 6s injection timeout
            pending_late.append((tid, time.time() + 7.0))

def main():
    stop = threading.Event()
    threading.Thread(target=vendor, args=(stop,), daemon=True).start()
    threading.Thread(target=dtu, args=(stop,), daemon=True).start()
    env = dict(os.environ, VENDOR_IP="127.0.0.1", VENDOR_PORT="15997", LISTEN_PORT="15002",
               HTTP_PORT="18093", DNS_PORT="0", CLOUD_IDLE_TIMEOUT="0", DTU_IDLE_TIMEOUT="0",
               LOG_LEVEL="INFO")
    proc = subprocess.Popen([sys.executable, "-m", "bridge.main"], cwd=BRIDGE_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)
    try:
        with urllib.request.urlopen("http://127.0.0.1:18093/api/read?addr=0x64&count=2", timeout=15) as r:
            print("unexpected success:", json.loads(r.read()))
    except Exception as err:
        print("injection timed out as expected:", type(err).__name__)
    time.sleep(4)  # let the late answer arrive
    with urllib.request.urlopen("http://127.0.0.1:18093/health", timeout=10) as r:
        h = json.loads(r.read())
    print("late_responses_swallowed:", h["late_responses_swallowed"],
          "| desync:", h["desync_bytes"], "| vendor bytes:", len(received_by_vendor))
    # the late answer would be a frame with a 0x8xxx transaction id
    leaked = any(received_by_vendor[i] == 0x80 and received_by_vendor[i+1] != 0
                 for i in range(0, max(0, len(received_by_vendor) - 1), 2))
    ok = h["late_responses_swallowed"] >= 1
    print("LATE RESPONSE TEST:", "PASS" if ok else "FAIL")
    stop.set(); proc.terminate()
    try: out = proc.communicate(timeout=5)[0]
    except Exception: out = ""
    if not ok: print(out[-1500:])
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
