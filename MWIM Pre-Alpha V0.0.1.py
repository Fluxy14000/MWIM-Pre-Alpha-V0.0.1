import os
import re
import struct
import zlib
import gzip
import subprocess
import sys
import threading
import queue
import csv
import json
import csv
import json
import tempfile
import datetime
from collections import Counter
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Minecraft MCA Analyzer — FINAL"
SECTOR = 4096
HEADER = SECTOR * 2

# NBT tag IDs
TAG_END=0
TAG_BYTE=1
TAG_SHORT=2
TAG_INT=3
TAG_LONG=4
TAG_FLOAT=5
TAG_DOUBLE=6
TAG_BYTE_ARRAY=7
TAG_STRING=8
TAG_LIST=9
TAG_COMPOUND=10
TAG_INT_ARRAY=11
TAG_LONG_ARRAY=12

# We do not parse huge block-state arrays unless necessary.
# The analyzer extracts the metadata needed for finding bases:
# coordinates, inhabited time, block entities, entities, status, DataVersion, timestamps.

def read_u8(d,p):
    if p >= len(d): raise ValueError("NBT tronqué")
    return d[p], p+1

def read_i32(d,p):
    if p+4 > len(d): raise ValueError("NBT tronqué (int)")
    return struct.unpack_from(">i",d,p)[0], p+4

def read_i64(d,p):
    if p+8 > len(d): raise ValueError("NBT tronqué (long)")
    return struct.unpack_from(">q",d,p)[0], p+8

def read_string(d,p):
    if p+2 > len(d): raise ValueError("NBT tronqué (string)")
    n=struct.unpack_from(">H",d,p)[0]
    p+=2
    if p+n > len(d): raise ValueError("NBT tronqué (string)")
    return d[p:p+n].decode("utf-8","replace"), p+n

def skip_payload(d,p,t):
    if t==TAG_BYTE: return checked_end(d,p,1)
    if t==TAG_SHORT: return checked_end(d,p,2)
    if t==TAG_INT: return checked_end(d,p,4)
    if t==TAG_LONG: return checked_end(d,p,8)
    if t==TAG_FLOAT: return checked_end(d,p,4)
    if t==TAG_DOUBLE: return checked_end(d,p,8)
    if t==TAG_BYTE_ARRAY:
        n,p=read_i32(d,p); return checked_end(d,p,max(0,n))
    if t==TAG_STRING:
        return read_string(d,p)[1]
    if t==TAG_LIST:
        subtype,p=read_u8(d,p)
        n,p=read_i32(d,p)
        for _ in range(max(0,n)):
            p=skip_payload(d,p,subtype)
        return p
    if t==TAG_COMPOUND:
        while True:
            typ,p=read_u8(d,p)
            if typ==TAG_END: return p
            _,p=read_string(d,p)
            p=skip_payload(d,p,typ)
    if t==TAG_INT_ARRAY:
        n,p=read_i32(d,p); return checked_end(d,p,4*max(0,n))
    if t==TAG_LONG_ARRAY:
        n,p=read_i32(d,p); return checked_end(d,p,8*max(0,n))
    raise ValueError(f"NBT tag inconnu: {t}")

def checked_end(d, p, size):
    end=p+size
    if p<0 or size<0 or end>len(d):
        raise ValueError("NBT tronqué")
    return end

def parse_payload(d,p,t,depth=0,max_depth=80):
    if depth>max_depth:
        raise ValueError("profondeur NBT excessive")
    if t==TAG_BYTE:
        v,p=read_u8(d,p); return v,p
    if t==TAG_SHORT:
        if p+2>len(d): raise ValueError("NBT tronqué")
        return struct.unpack_from(">h",d,p)[0],p+2
    if t==TAG_INT: return read_i32(d,p)
    if t==TAG_LONG: return read_i64(d,p)
    if t==TAG_FLOAT:
        if p+4>len(d): raise ValueError("NBT tronqué")
        return struct.unpack_from(">f",d,p)[0],p+4
    if t==TAG_DOUBLE:
        if p+8>len(d): raise ValueError("NBT tronqué")
        return struct.unpack_from(">d",d,p)[0],p+8
    if t==TAG_BYTE_ARRAY:
        n,p=read_i32(d,p)
        if n<0: raise ValueError("longueur de byte array négative")
        end=checked_end(d,p,n)
        return d[p:end],end
    if t==TAG_STRING: return read_string(d,p)
    if t==TAG_LIST:
        subtype,p=read_u8(d,p); n,p=read_i32(d,p)
        if n<0: raise ValueError("longueur de liste négative")
        out=[]
        for _ in range(max(0,n)):
            v,p=parse_payload(d,p,subtype,depth+1,max_depth); out.append(v)
        return out,p
    if t==TAG_COMPOUND:
        out={}
        while True:
            typ,p=read_u8(d,p)
            if typ==TAG_END: return out,p
            name,p=read_string(d,p)
            v,p=parse_payload(d,p,typ,depth+1,max_depth)
            out[name]=v
    if t==TAG_INT_ARRAY:
        n,p=read_i32(d,p)
        if n<0: raise ValueError("longueur de int array négative")
        out=[]
        for _ in range(n):
            v,p=read_i32(d,p); out.append(v)
        return out,p
    if t==TAG_LONG_ARRAY:
        n,p=read_i32(d,p)
        if n<0: raise ValueError("longueur de long array négative")
        out=[]
        for _ in range(n):
            v,p=read_i64(d,p); out.append(v)
        return out,p
    raise ValueError(f"NBT tag inconnu: {t}")

def parse_nbt(d):
    typ,_=read_u8(d,0)
    if typ!=TAG_COMPOUND:
        raise ValueError("Racine NBT invalide")
    _,p=read_string(d,1)
    result,end=parse_payload(d,p,TAG_COMPOUND)
    if end != len(d):
        raise ValueError("données NBT supplémentaires après le compound racine")
    return result

def get_compound(root):
    # Old Anvil format used Level; modern Java chunk NBT is normally direct.
    if isinstance(root,dict) and isinstance(root.get("Level"),dict):
        return root["Level"]
    return root if isinstance(root,dict) else {}

def decompress_chunk(payload, compression, source_path=None, slot=None):
    if compression==1:
        return gzip.decompress(payload)
    if compression==2:
        return zlib.decompress(payload)
    if compression==3:
        return payload
    if compression==4:
        # Modern region files can use LZ4. Never install packages implicitly.
        try:
            import lz4.block
        except ImportError:
            raise RuntimeError(
                "Ce fichier utilise la compression LZ4 (type 4). "
                "Installe lz4 avec: py -m pip install lz4"
            )
        return lz4.block.decompress(payload)
    raise ValueError(f"compression inconnue ({compression})")

def region_coords(path):
    m=re.fullmatch(r"r\.(-?\d+)\.(-?\d+)\.mca",os.path.basename(path),re.I)
    if not m:
        raise ValueError("Nom attendu : r.X.Z.mca")
    return int(m.group(1)),int(m.group(2))

def entity_id(e):
    if not isinstance(e,dict): return "inconnu"
    # Entities normally use id; some block entity formats also expose id.
    v=e.get("id")
    return str(v) if v not in (None,"") else "inconnu"

def pos_of(e):
    if not isinstance(e,dict): return None
    p=e.get("Pos")
    if isinstance(p,list) and len(p)>=3:
        try: return tuple(float(x) for x in p[:3])
        except Exception: pass
    if all(k in e for k in ("x","y","z")):
        try: return (float(e["x"]),float(e["y"]),float(e["z"]))
        except Exception: pass
    return None

def chunk_info(raw, rx, rz, local_x, local_z, compression, slot):
    root=parse_nbt(raw)
    data=get_compound(root)

    inh=data.get("InhabitedTime", root.get("InhabitedTime") if isinstance(root,dict) else None)
    if inh is not None:
        try: inh=int(inh)
        except Exception: inh=None
    inh_available = "InhabitedTime" in data or (isinstance(root,dict) and "InhabitedTime" in root)

    bes=data.get("block_entities", data.get("TileEntities",[]))
    if not isinstance(bes,list): bes=[]
    ents=data.get("entities", data.get("Entities",[]))
    if not isinstance(ents,list): ents=[]

    # In modern worlds these are useful when present.
    status=data.get("Status","")
    if not isinstance(status,str): status=str(status)
    data_version=data.get("DataVersion", root.get("DataVersion") if isinstance(root,dict) else None)
    try: data_version=int(data_version) if data_version is not None else None
    except Exception: data_version=None

    cx=rx*32+local_x
    cz=rz*32+local_z

    # Count useful storage / machine types.
    bec=Counter(entity_id(x) for x in bes)
    entc=Counter(entity_id(x) for x in ents)

    return {
        "local_x":local_x,"local_z":local_z,
        "x":cx,"z":cz,
        "inh":inh if inh_available else None,"be":bes,"en":ents,
        "timestamp":None,
        "be_counts":bec,"ent_counts":entc,
        "status":status,"data_version":data_version,
        "compression":compression,"slot":slot
    }

def read_mca(path, progress=None):
    with open(path,"rb") as f:
        data=f.read()
    if len(data)<HEADER:
        raise ValueError("Fichier trop petit pour un .mca valide.")

    rx,rz=region_coords(path)
    locations=[]
    for i in range(1024):
        base=i*4
        off=(data[base]<<16)|(data[base+1]<<8)|data[base+2]
        sectors=data[base+3]
        locations.append((off,sectors))

    chunks=[]
    errors=[]
    compression_counts=Counter()
    timestamps=[struct.unpack_from(">I",data,SECTOR+i*4)[0] for i in range(1024)]
    occupied=[]
    for i,(off,sectors) in enumerate(locations):
        if not off and not sectors: continue
        if not off or not sectors:
            errors.append((i,"entrée d'en-tête incohérente (offset/taille nulle)") )
            continue
        if off < 2 or off+sectors > (len(data)+SECTOR-1)//SECTOR:
            errors.append((i,"secteurs déclarés hors du fichier"))
            continue
        occupied.append((off,off+sectors,i))
    occupied.sort()
    for (_,end,prev), (start,_,cur) in zip(occupied,occupied[1:]):
        if start < end:
            errors.append((cur,f"secteurs chevauchent le slot {prev}"))
            errors.append((prev,f"secteurs chevauchent le slot {cur}"))
    invalid_slots={slot for slot,msg in errors}

    for i,(off,sectors) in enumerate(locations):
        if not off or not sectors or i in invalid_slots:
            continue
        start=off*SECTOR
        try:
            if start+5>len(data):
                raise ValueError("offset hors fichier")
            length=struct.unpack_from(">I",data,start)[0]
            if length<1:
                raise ValueError("longueur de chunk invalide")
            if length > sectors*SECTOR-4:
                raise ValueError("longueur de chunk supérieure aux secteurs alloués")
            end=start+4+length
            if end>len(data):
                raise ValueError("chunk dépasse la taille du fichier")
            compression_byte=data[start+4]
            external=bool(compression_byte & 0x80)
            compression=compression_byte & 0x7f
            compression_counts[compression]+=1
            if external:
                sidecar=os.path.join(os.path.dirname(path),f"c.{rx*32+i%32}.{rz*32+i//32}.mcc")
                with open(sidecar,"rb") as sf: payload=sf.read()
            else:
                payload=data[start+5:end]
            raw=decompress_chunk(payload,compression,path,i)
            c=chunk_info(raw,rx,rz,i%32,i//32,compression,i)
            c["timestamp"]=timestamps[i] if timestamps[i] else None
            chunks.append(c)
        except Exception as e:
            errors.append((i,str(e)))

        if progress and (i%16==0 or i==1023):
            progress(i+1,1024)

    present_slots=sum(1 for off,sec in locations if off and sec)
    return {
        "rx":rx,"rz":rz,"chunks":chunks,"errors":errors,
        "present_slots":present_slots,
        "timestamps":timestamps,
        "compression_counts":compression_counts,
        "file_size":len(data)
    }

def score_chunk(c):
    # Heuristic only. It is deliberately not called "proof of base".
    storage=len(c["be"])
    entities=len(c["en"])
    active=c["inh"] or 0
    valuable=sum(v for k,v in c["be_counts"].items()
                 if any(s in k.lower() for s in
                        ("chest","barrel","shulker","furnace","hopper","dispenser",
                         "dropper","brewing","beacon","ender_chest","spawner",
                         "crafter","sign","lectern")))
    return active + storage*5000 + valuable*8000 + entities*250

def center_block(c):
    # Bloc central du chunk : un chunk fait 16x16 blocs, le milieu est à +8.
    return c["x"]*16+8, c["z"]*16+8

def analyze_result(r):
    ch=r["chunks"]
    r["top_inh"]=sorted((c for c in ch if c["inh"] is not None),key=lambda c:c["inh"],reverse=True)
    r["top_score"]=sorted(ch,key=score_chunk,reverse=True)
    r["total_be"]=sum(len(c["be"]) for c in ch)
    r["total_ent"]=sum(len(c["en"]) for c in ch)
    r["be_counter"]=Counter()
    r["ent_counter"]=Counter()
    for c in ch:
        r["be_counter"].update(c["be_counts"])
        r["ent_counter"].update(c["ent_counts"])
    r["best"]=r["top_inh"][0] if r["top_inh"] else None
    return r

class App:
    def __init__(self, root):
        self.root=root
        self.root.title(APP_TITLE)
        self.root.geometry("1350x850")
        self.root.minsize(1050,700)
        self.files=[]
        self.results=[]
        self.q=queue.Queue()
        self.worker=None

        top=ttk.Frame(root,padding=10); top.pack(fill="x")
        ttk.Label(top,text=APP_TITLE,font=("Segoe UI",18,"bold")).pack(side="left")
        for txt,cmd in [
            ("Ajouter MCA",self.add_files),
            ("Dossier region",self.add_folder),
            ("Vider",self.clear)
        ]:
            ttk.Button(top,text=txt,command=cmd).pack(side="right",padx=3)

        bar=ttk.Frame(root,padding=(10,0,10,8)); bar.pack(fill="x")
        ttk.Button(bar,text="ANALYSER",command=self.start_analysis).pack(side="left",padx=3)
        ttk.Button(bar,text="Exporter rapport",command=self.export).pack(side="left",padx=3)
        ttk.Button(bar,text="Copier",command=self.copy).pack(side="left",padx=3)
        self.status=tk.StringVar(value="Ajoute un ou plusieurs fichiers .mca.")
        ttk.Label(bar,textvariable=self.status).pack(side="left",padx=15)

        self.progress=ttk.Progressbar(bar,length=240,mode="determinate")
        self.progress.pack(side="right")

        pan=ttk.Panedwindow(root,orient="horizontal"); pan.pack(fill="both",expand=True,padx=10,pady=5)
        lf=ttk.Frame(pan,padding=5); rf=ttk.Frame(pan,padding=5)
        pan.add(lf,weight=1); pan.add(rf,weight=5)

        ttk.Label(lf,text="Fichiers à analyser",font=("Segoe UI",11,"bold")).pack(anchor="w")
        self.lb=tk.Listbox(lf,selectmode="extended")
        self.lb.pack(fill="both",expand=True,pady=5)

        ttk.Label(rf,text="Régions",font=("Segoe UI",11,"bold")).pack(anchor="w")
        cols=("region","chunks","inh","best","be","ent","errors")
        self.tree=ttk.Treeview(rf,columns=cols,show="headings",height=11)
        names={
            "region":"Région","chunks":"Chunks lus","inh":"Max InhabitedTime",
            "best":"Chunk le + actif","be":"Block entities","ent":"Entités","errors":"Erreurs"
        }
        widths={"region":100,"chunks":90,"inh":155,"best":170,"be":115,"ent":85,"errors":75}
        self.col_names=names
        self.sort_state={"col":None,"rev":False}
        for c in cols:
            self.tree.heading(c,text=names[c],command=lambda c=c:self.sort_by(c))
            self.tree.column(c,width=widths[c],anchor="center")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>",self.select_result)

        ttk.Label(rf,text="Détails",font=("Segoe UI",11,"bold")).pack(anchor="w",pady=(10,3))
        txtframe=ttk.Frame(rf); txtframe.pack(fill="both",expand=True)
        self.txt=tk.Text(txtframe,wrap="none",font=("Consolas",10))
        sy=ttk.Scrollbar(txtframe,orient="vertical",command=self.txt.yview)
        sx=ttk.Scrollbar(txtframe,orient="horizontal",command=self.txt.xview)
        self.txt.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.txt.pack(side="left",fill="both",expand=True)
        sy.pack(side="right",fill="y")
        sx.pack(side="bottom",fill="x")

        root.after(100,self.poll)

    def add_files(self):
        fs=filedialog.askopenfilenames(filetypes=[("Minecraft region files","*.mca")])
        for f in fs:
            if f not in self.files:
                self.files.append(f); self.lb.insert("end",f)
        self.status.set(f"{len(self.files)} fichier(s) dans la liste.")

    def add_folder(self):
        d=filedialog.askdirectory(title="Sélectionner le dossier region")
        if not d:return
        fs=[]
        for root,dirs,files in os.walk(d):
            for f in files:
                if re.fullmatch(r"r\.-?\d+\.-?\d+\.mca",f,re.I):
                    fs.append(os.path.join(root,f))
        for f in sorted(fs):
            if f not in self.files:
                self.files.append(f); self.lb.insert("end",f)
        self.status.set(f"{len(fs)} fichier(s) trouvés — {len(self.files)} au total.")

    def clear(self):
        self.files=[]; self.results=[]; self.lb.delete(0,"end")
        for x in self.tree.get_children(): self.tree.delete(x)
        self.txt.delete("1.0","end")
        self.progress["value"]=0
        self.status.set("Liste vidée.")

    def start_analysis(self):
        if not self.files:
            messagebox.showwarning("Aucun fichier","Ajoute des fichiers .mca ou un dossier region.")
            return
        if self.worker and self.worker.is_alive():
            return
        self.results=[]
        for x in self.tree.get_children(): self.tree.delete(x)
        self.txt.delete("1.0","end")
        self.progress["value"]=0
        self.worker=threading.Thread(target=self.worker_run,daemon=True)
        self.worker.start()

    def worker_run(self):
        total=len(self.files)
        for n,path in enumerate(self.files,1):
            self.q.put(("status",f"Analyse {n}/{total} : {os.path.basename(path)}"))
            try:
                r=read_mca(path,lambda a,b:self.q.put(("progress",a,b)))
                r["path"]=path
                analyze_result(r)
                self.q.put(("result",r))
            except Exception as e:
                self.q.put(("fatal",path,str(e)))
        self.q.put(("done",))

    def poll(self):
        try:
            while True:
                item=self.q.get_nowait()
                kind=item[0]
                if kind=="status":
                    self.status.set(item[1])
                elif kind=="progress":
                    a,b=item[1],item[2]
                    self.progress["value"]=a/b*100
                elif kind=="result":
                    self.add_result(item[1])
                elif kind=="fatal":
                    path,err=item[1],item[2]
                    r={
                        "path":path,"rx":None,"rz":None,"chunks":[],"errors":[(-1,err)],
                        "present_slots":0,"compression_counts":Counter(),
                        "file_size":0,"top_inh":[],"top_score":[],"total_be":0,
                        "total_ent":0,"be_counter":Counter(),"ent_counter":Counter(),"best":None
                    }
                    self.results.append(r)
                    self.tree.insert("", "end",iid=str(len(self.results)-1),
                                     values=("ERREUR","0/1024","0","-",0,0,1))
                elif kind=="done":
                    self.status.set(f"Terminé : {len(self.results)} région(s).")
                    self.progress["value"]=100
                    if self.sort_state["col"]: self.sort_by(self.sort_state["col"],toggle=False)
                    if self.results and not self.tree.selection():
                        iid=self.tree.get_children()[0]
                        self.tree.selection_set(iid); self.show(int(iid))
        except queue.Empty:
            pass
        self.root.after(100,self.poll)

    def add_result(self,r):
        self.results.append(r)
        i=len(self.results)-1
        b=r["best"]
        self.tree.insert("", "end",iid=str(i),values=(
            f"{r['rx']},{r['rz']}",
            f"{len(r['chunks'])}/{r['present_slots']}",
            f"{b['inh']:,}" if b and b["inh"] is not None else "inconnu",
            f"{b['x']},{b['z']}" if b else "-",
            r["total_be"],r["total_ent"],len(r["errors"])
        ))

    def sort_value(self,col,r):
        # Valeur numérique réelle (pas le texte affiché) ; None = donnée absente.
        b=r["best"]
        if col=="region": return (r["rx"],r["rz"]) if r["rx"] is not None else None
        if col=="chunks": return (len(r["chunks"]),r["present_slots"])
        if col=="inh":    return b["inh"] if b and b["inh"] is not None else None
        if col=="best":   return (b["x"],b["z"]) if b else None
        if col=="be":     return r["total_be"]
        if col=="ent":    return r["total_ent"]
        if col=="errors": return len(r["errors"])
        return None

    def sort_by(self,col,toggle=True):
        st=self.sort_state
        if toggle:
            if st["col"]==col: st["rev"]=not st["rev"]
            else:
                st["col"]=col
                # 1er clic : croissant pour les coordonnées, décroissant pour les compteurs
                st["rev"]= col not in ("region","best")
        items=[(self.sort_value(col,self.results[int(i)]),i) for i in self.tree.get_children()]
        ok=sorted([x for x in items if x[0] is not None],key=lambda x:x[0],reverse=st["rev"])
        missing=[x for x in items if x[0] is None]   # toujours en bas
        for pos,(_,iid) in enumerate(ok+missing):
            self.tree.move(iid,"",pos)
        for c,name in self.col_names.items():
            arrow=(" ▼" if st["rev"] else " ▲") if c==col else ""
            self.tree.heading(c,text=name+arrow)

    def select_result(self,event=None):
        s=self.tree.selection()
        if s:self.show(int(s[0]))

    def show(self,i):
        r=self.results[i]
        t=[]
        t += ["="*100,
              f"FICHIER : {os.path.basename(r['path'])}",
              f"RÉGION  : X={r['rx']}, Z={r['rz']}" if r["rx"] is not None else "RÉGION : inconnue",
              "="*100,"",
              "COUVERTURE",
              f"Chunks région : X={r['rx']*32} -> {r['rx']*32+31}" if r["rx"] is not None else "-",
              f"                Z={r['rz']*32} -> {r['rz']*32+31}" if r["rz"] is not None else "-",
              f"Blocs          : X={r['rx']*512} -> {r['rx']*512+511}" if r["rx"] is not None else "-",
              f"                Z={r['rz']*512} -> {r['rz']*512+511}" if r["rz"] is not None else "-",
              "",
              "STATISTIQUES RÉELLES",
              f"Slots occupés dans l'en-tête : {r['present_slots']} / 1024",
              f"Chunks NBT lus avec succès  : {len(r['chunks'])}",
              f"Chunks en erreur             : {len(r['errors'])}",
              f"Block entities               : {r['total_be']}",
              f"Entités                      : {r['total_ent']}",
              f"Taille du fichier            : {r['file_size']:,} octets",
              "",
              "COMPRESSIONS",
              "-"*100]
        compnames={1:"GZIP",2:"ZLIB",3:"NON COMPRESSÉ",4:"LZ4"}
        for k,v in sorted(r["compression_counts"].items()):
            t.append(f"type {k}: {compnames.get(k,'INCONNU')} — {v} chunk(s)")

        t += ["","TOP 20 — INHABITEDTIME (uniquement si la donnée existe)","-"*100]
        if not r["top_inh"]:
            t.append("Aucun chunk analysé ne contient InhabitedTime.")
        for n,c in enumerate(r["top_inh"][:20],1):
            hours=c["inh"]/72000
            cx_,cz_=center_block(c)
            t.append(f"{n:2}. Région locale ({c['local_x']}, {c['local_z']}) | chunk ({c['x']}, {c['z']}) | statistique=InhabitedTime | {c['inh']:,} ticks | {hours:.2f} h | BE={len(c['be'])} | Entités={len(c['en'])} | Status={c['status'] or 'inconnu'} | DataVersion={c['data_version'] if c['data_version'] is not None else 'inconnue'}")
            t.append(f"    Bloc central X/Z : {cx_} {cz_} | /tp @s {cx_} ~ {cz_} (Y relatif, conservé à l'altitude actuelle)")

        t += ["","BLOCK ENTITIES DÉTECTÉES","-"*100]
        if r["be_counter"]:
            for k,v in r["be_counter"].most_common(50):
                t.append(f"{v:6}  {k}")
        else:t.append("Aucune block entity.")

        t += ["","ENTITÉS DÉTECTÉES","-"*100]
        if r["ent_counter"]:
            for k,v in r["ent_counter"].most_common(50):
                t.append(f"{v:6}  {k}")
        else:t.append("Aucune entité.")

        t += ["","TOP 20 — CANDIDATS POUR UNE ZONE DE BASE","-"*100,
              "Rang par score heuristique : InhabitedTime + pondérations BE/entités. Ce score ne prouve PAS une base."]
        for n,c in enumerate(r["top_score"][:20],1):
            bx=c["x"]*16; bz=c["z"]*16
            t.append(f"{n:2}. Région locale ({c['local_x']}, {c['local_z']}) | Chunk ({c['x']}, {c['z']}) | blocs X={bx}..{bx+15} Z={bz}..{bz+15} | "
                     f"score={score_chunk(c):,} | InhabitedTime={c['inh'] if c['inh'] is not None else 'inconnu'} | BE={len(c['be'])} | Entités={len(c['en'])}")
            cx_,cz_=center_block(c)
            t.append(f"    Bloc central X/Z : {cx_} {cz_} | /tp @s {cx_} ~ {cz_} (Y relatif, conservé à l'altitude actuelle)")

        if r["best"]:
            b=r["best"]; bx=b["x"]*16; bz=b["z"]*16
            t += ["","CHUNK LE PLUS ACTIF","-"*100,
                  f"Chunk         : ({b['x']}, {b['z']})",
                  f"Blocs         : X={bx} -> {bx+15}",
                  f"                Z={bz} -> {bz+15}",
                  f"InhabitedTime : {b['inh']:,} ticks" if b['inh'] is not None else "InhabitedTime : inconnue",
                  f"Temps approx. : {b['inh']/72000:.2f} heures" if b['inh'] is not None else "Temps approx. : inconnu",
                  f"Block entities: {len(b['be'])}",
                  f"Entités       : {len(b['en'])}",
                  f"Status        : {b['status'] or 'inconnu'}",
                  f"DataVersion   : {b['data_version'] if b['data_version'] is not None else 'inconnue'}",
                  f"Timestamp MCA : {b['timestamp'] if b['timestamp'] is not None else 'inconnu'} (secondes Unix)"]

        if r["errors"]:
            t += ["","ERREURS DE LECTURE","-"*100]
            for slot,e in r["errors"][:50]:
                t.append(f"slot {slot}: {e}")

        self.txt.delete("1.0","end")
        self.txt.insert("1.0","\n".join(t))

    def report(self):
        return "\n\n\n".join(self.report_one(r) for r in self.results)

    def report_one(self,r):
        old=self.txt.get("1.0","end")
        i=self.results.index(r)
        self.show(i)
        s=self.txt.get("1.0","end").rstrip()
        self.txt.delete("1.0","end"); self.txt.insert("1.0",old)
        return s

    def export(self):
        if not self.results:
            messagebox.showwarning("Rien à exporter","Analyse d'abord des fichiers.")
            return
        p=filedialog.asksaveasfilename(defaultextension=".txt",
                                       filetypes=[("Rapport texte","*.txt"),("Données JSON","*.json"),("Données CSV","*.csv")])
        if p:
            ext=os.path.splitext(p)[1].lower()
            if ext in (".json", ".csv"):
                rows=[]
                for region in self.results:
                    for c in region["chunks"]:
                        x,z=center_block(c)
                        rows.append({
                            "region_x":region["rx"],"region_z":region["rz"],"slot":c["slot"],
                            "chunk_x":c["x"],"chunk_z":c["z"],"inhabited_time_ticks":c["inh"],
                            "status":c["status"] or None,"data_version":c["data_version"],
                            "timestamp_unix":c["timestamp"],"compression":c["compression"],
                            "block_entities":len(c["be"]),"entities":len(c["en"]),
                            "block_x":x,"block_z":z,"teleport_command":f"/tp @s {x} ~ {z}",
                            "score":score_chunk(c)
                        })
                with open(p,"w",encoding="utf-8-sig" if ext==".csv" else "utf-8",newline="" if ext==".csv" else None) as f:
                    if ext==".json": json.dump({"regions":len(self.results),"chunks":rows},f,ensure_ascii=False,indent=2)
                    elif rows:
                        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
                        writer.writeheader(); writer.writerows(rows)
                    else: f.write("Aucune donnée de chunk analysée.\n")
            else:
                with open(p,"w",encoding="utf-8") as f:f.write(self.report())
            messagebox.showinfo("Export terminé",f"Rapport enregistré :\n{p}")

    def copy(self):
        if not self.results:return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.report())
        self.status.set("Rapport complet copié dans le presse-papiers.")

if __name__=="__main__":
    root=tk.Tk()
    App(root)
    root.mainloop()
