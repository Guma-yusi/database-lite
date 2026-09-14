import argparse
import json
from pathlib import Path
from os_sim import StorageService
from os_sim.page_file import PAYLOAD_SIZE


def fixed_payload(text):
    raw=text.encode("utf-8")
    if len(raw)>PAYLOAD_SIZE:raise ValueError("文本超过单页容量")
    return raw+bytes(PAYLOAD_SIZE-len(raw))


def main():
    parser=argparse.ArgumentParser(description="MiniDB 独立 OS 存储仿真实体")
    parser.add_argument("--data",default="os_sim_data");parser.add_argument("--policy",choices=["FIFO","LFU","LRU","CLOCK"],default="LRU");parser.add_argument("--cache-pages",type=int,default=8)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("status");sub.add_parser("allocate")
    release=sub.add_parser("release");release.add_argument("page_id",type=int)
    read=sub.add_parser("read");read.add_argument("page_id",type=int)
    write=sub.add_parser("write");write.add_argument("page_id",type=int);write.add_argument("text")
    sub.add_parser("checkpoint")
    args=parser.parse_args()
    store=StorageService(Path(args.data),args.cache_pages,args.policy)
    try:
        if args.command=="status":
            result={
                "stats":store.stats(),
                "allocated":[p for p in store.page_directory() if p["allocated"]],
                "frames":[{"page_id":f.page_id,"generation":f.generation,"dirty":f.dirty,"pin_count":f.pin_count,"page_lsn":f.page_lsn} for f in store.cache.frames.values()],
                "events":store.cache.recent_events(30),
            }
        elif args.command=="allocate":result={"page_id":store.allocate_page()}
        elif args.command=="release":store.release_page(args.page_id);result={"released":args.page_id}
        elif args.command=="read":result={"page_id":args.page_id,"text":store.read_page(args.page_id).rstrip(b"\0").decode("utf-8")}
        elif args.command=="write":result={"page_id":args.page_id,"lsn":store.write_page(args.page_id,fixed_payload(args.text))}
        else:result=store.checkpoint()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:store.close()


if __name__=="__main__":main()
