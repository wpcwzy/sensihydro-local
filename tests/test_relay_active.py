"""Test that an active vendor keeps the link alive and heartbeats are forwarded."""
import json, os, socket, subprocess, sys, threading, time, urllib.request

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/sensihydro-local"

def vendor(stop):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 15998)); srv.listen(5)
    conns = []
    def run():
        while not stop.is_set():
            try: c, _ = srv.accept()
            except OSError: return
            conns.append(c)
            threading.Thread(target=_drain, args=(c,), daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.5)
    while not stop.is_set():
        for c in list(conns):
            try: c.sendall(b"\x00\x01\x00\x00\x00\x06\x01\x03\x40\x00\x00\x01")
            except OSError: conns.remove(c)
        time.sleep(1)

def _drain(sock):
    try:
        while sock.recv(4096): pass
    except OSError: pass

def dtu(stop, received):
    time.sleep(1.5)
    c = socket.create_connection(("127.0.0.1", 15001), timeout=5)
    c.sendall(b"0034EAE724862A")
    c.settimeout(0.5); buf=b""
    while not stop.is_set():
        try: data=c.recv(4096)
        except socket.timeout:
            try: c.sendall(b"0034EAE724862A")
            except OSError: break
            continue
        except OSError: break
        if not data: break
        buf+=data
        while len(buf)>=6:
            ln=(buf[4]<<8)|buf[5]
            if len(buf)<6+ln: break
            f,buf=buf[:6+ln],buf[6+ln:]
            tid,pdu=f[:2],f[6:]
            received.append(f)
            if pdu[1]==3:
                n=(pdu[4]<<8)|pdu[5]
                payload=b"".join((1234+i).to_bytes(2,"big") for i in range(n))
                body=bytes([1,3,len(payload)])+payload
            else: body=pdu[:6]
            c.sendall(tid+b"\x00\x00"+len(body).to_bytes(2,"big")+body)

def main():
    stop=threading.Event(); received=[]
    threading.Thread(target=vendor,args=(stop,),daemon=True).start()
    threading.Thread(target=dtu,args=(stop,received),daemon=True).start()
    env=dict(os.environ, VENDOR_IP="127.0.0.1", VENDOR_PORT="15998", LISTEN_PORT="15001",
             HTTP_PORT="18091", DNS_PORT="0", CLOUD_IDLE_TIMEOUT="3", DTU_IDLE_TIMEOUT="2", LOG_LEVEL="INFO")
    proc=subprocess.Popen([sys.executable,"-m","bridge.main"],cwd=BRIDGE_DIR,env=env,
                          stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    time.sleep(4)
    def health():
        with urllib.request.urlopen("http://127.0.0.1:18091/health",timeout=10) as r: return json.loads(r.read())
    try:
        with urllib.request.urlopen("http://127.0.0.1:18091/api/read?addr=0x64&count=3",timeout=10) as r:
            print("read:",json.loads(r.read()))
    except Exception as e: print("read failed:",e)
    time.sleep(2); h=health()
    print("cloud_frames:",h["cloud_frames"],"dtu_frames:",h["dtu_frames"],"heartbeats:",h["dtu_heartbeats"],
          "watchdog_drops:",h["watchdog_drops"],"desync:",h["desync_bytes"],"late:",h["late_responses_swallowed"])
    ok = h["cloud_frames"]>0 and h["dtu_heartbeats"]>0 and h["watchdog_drops"]==0
    print("ACTIVE LINK TEST:","PASS" if ok else "FAIL")
    stop.set(); proc.terminate()
    try: out=proc.communicate(timeout=5)[0]
    except Exception: out=""
    if not ok: print(out[-2000:])
    return 0 if ok else 1

if __name__=="__main__": raise SystemExit(main())
