#!/usr/bin/env python3
"""Kibana/Elasticsearch 日志查询脚本 - 完全开放版

支持两种模式：
1. 简化模式：通过参数快速查询
2. 原生模式：直接传 ES 请求体，完全开放 ES 能力
"""

import json
import urllib.request
import argparse
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

ES_URL = "http://elasticsearch.yamibuy.net"

# 默认请求体文件名 (固定)
BODY_FILE_NAME = "kibana-query.json"
BODY_DIR_NAME = ".query-kibana-logs"


def es_request(url: str, data: dict = None, method: str = None, timeout: int = 30) -> dict:
    """执行 ES 请求"""
    if data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    if method:
        req.method = method
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_all_services() -> list:
    """获取所有可用服务名"""
    url = f"{ES_URL}/_cat/indices?h=index&format=json"
    indices = es_request(url, timeout=10)
    services = set()
    for idx in indices:
        name = idx.get("index", "")
        if name.startswith("k8s-") and "-log-" in name:
            svc = name.replace("k8s-", "").split("-log-")[0]
            services.add(svc)
    return sorted(services)


def match_services(pattern: str, all_services: list) -> list:
    """模糊匹配服务名"""
    if not pattern:
        return []
    pattern_lower = pattern.lower()
    matched = [s for s in all_services if pattern_lower in s.lower()]
    exact = [s for s in matched if s.lower() == pattern_lower]
    return exact if exact else matched


def build_index_pattern(services: list = None, order_mode: bool = False) -> str:
    """构建索引模式"""
    if services:
        return ",".join(f"k8s-{s}-log-*" for s in services)
    if order_mode:
        return "k8s-*-so-*-log-*,k8s-*-rma-*-log-*"
    return "k8s-*-log-*"


def parse_time_range(time_range: str = None, start: str = None, end: str = None) -> tuple:
    """解析时间范围"""
    if start:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                start_dt = datetime.strptime(start, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析开始时间: {start}")

        if end:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    end_dt = datetime.strptime(end, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"无法解析结束时间: {end}")
        else:
            end_dt = datetime.utcnow()
        return start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    now = datetime.utcnow()
    time_range = time_range or "1h"
    unit = time_range[-1]
    value = int(time_range[:-1])
    delta_map = {'m': timedelta(minutes=value), 'h': timedelta(hours=value), 'd': timedelta(days=value)}
    delta = delta_map.get(unit, timedelta(hours=1))
    return (now - delta).strftime("%Y-%m-%dT%H:%M:%S.000Z"), now.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def raw_query(index: str, body: dict, endpoint: str = "_search") -> dict:
    """
    原生查询 - 直接发送 ES 请求体
    
    Args:
        index: 索引模式
        body: ES 请求体 (query/aggs/size/sort 等)
        endpoint: ES 端点，默认 _search，也可以是 _count/_msearch 等
    """
    url = f"{ES_URL}/{index}/{endpoint}"
    return es_request(url, body)


def simple_search(
    index: str = None,
    services: list = None,
    keywords: list = None,
    order_sn: str = None,
    level: str = None,
    time_range: str = None,
    start: str = None,
    end: str = None,
    limit: int = 50
) -> dict:
    """简化搜索 - 通过参数构建查询"""
    start_ts, end_ts = parse_time_range(time_range, start, end)
    
    # 优先使用传入的 index，否则根据 services 构建
    if not index:
        index = build_index_pattern(services, order_mode=bool(order_sn and not services))
    
    must_clauses = [{"range": {"@timestamp": {"gte": start_ts, "lte": end_ts}}}]
    
    if keywords:
        for kw in keywords:
            must_clauses.append({"match_phrase": {"message": kw}})
    
    if order_sn:
        must_clauses.append({"match_phrase": {"message": order_sn}})
    
    if level:
        level_upper = level.upper()
        if level_upper == "ERROR":
            must_clauses.append({"bool": {"should": [
                {"match": {"message": "ERROR"}},
                {"match": {"message": "Exception"}},
                {"match": {"message": "FATAL"}}
            ], "minimum_should_match": 1}})
        else:
            must_clauses.append({"match": {"message": level_upper}})
    
    body = {
        "query": {"bool": {"must": must_clauses}},
        "size": limit,
        "sort": [{"@timestamp": "desc"}]
    }
    
    url = f"{ES_URL}/{index}/_search"
    return es_request(url, body)


def format_output(result: dict, output_format: str = "text") -> str:
    """格式化输出"""
    if output_format == "raw":
        return json.dumps(result, ensure_ascii=False, indent=2)
    
    # 处理聚合结果
    if "aggregations" in result:
        return json.dumps(result["aggregations"], ensure_ascii=False, indent=2)
    
    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {})
    total_count = total.get("value", len(hits)) if isinstance(total, dict) else total
    
    if not hits:
        return "未找到匹配的日志"
    
    if output_format == "json":
        logs = []
        for hit in hits:
            src = hit["_source"]
            logs.append({
                "timestamp": src.get("logdate", src.get("@timestamp", "")),
                "message": src.get("message", ""),
                "index": hit.get("_index", "")
            })
        return json.dumps({"total": total_count, "count": len(hits), "logs": logs}, ensure_ascii=False, indent=2)
    
    lines = []
    for hit in hits:
        src = hit["_source"]
        ts = src.get("logdate", src.get("@timestamp", ""))
        msg = src.get("message", "")
        if len(msg) > 500:
            msg = msg[:500] + "..."
        idx = hit.get("_index", "")
        svc = idx.replace("k8s-", "").split("-log-")[0] if idx.startswith("k8s-") else ""
        lines.append(f"[{ts}] [{svc}] {msg}")
    
    return f"找到 {total_count} 条日志 (显示 {len(hits)} 条):\n\n" + "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Kibana 日志查询 - 支持简化模式和原生 ES 查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
简化模式示例:
  %(prog)s -s so -k shipping            # 在 so 服务搜 shipping
  %(prog)s -o 2026020337619             # 按订单号搜索
  %(prog)s --level error -t 2h          # 最近2小时的错误日志

原生模式示例 (--body-file 从文件读取 ES 请求体):
  # 1. 将请求体写入 <工作区>/.query-kibana-logs/kibana-query.json
  # 2. 执行查询
  %(prog)s -s so --body-file /path/to/workspace

工具命令:
  %(prog)s --list-services              # 列出所有服务
  %(prog)s --show-index -s so           # 显示索引模式（不执行查询）
"""
    )
    # 索引相关
    parser.add_argument("-s", "--service", action="append", dest="services", metavar="SVC",
                        help="服务名 (支持模糊匹配，可多次指定)")
    parser.add_argument("--index", metavar="PATTERN",
                        help="直接指定索引模式 (覆盖 -s)")
    
    # 简化模式参数
    parser.add_argument("-k", "--keyword", action="append", dest="keywords", metavar="KW",
                        help="搜索关键词 (可多次指定)")
    parser.add_argument("-o", "--order", dest="order", metavar="ORDER_SN",
                        help="订单号")
    parser.add_argument("--level", choices=["error", "info", "debug", "ERROR", "INFO", "DEBUG"],
                        help="日志级别")
    parser.add_argument("-t", "--time-range", dest="time_range", metavar="RANGE",
                        help="相对时间范围 (15m/1h/24h/7d)")
    parser.add_argument("--start", metavar="TIME", help="开始时间")
    parser.add_argument("--end", metavar="TIME", help="结束时间")
    parser.add_argument("-l", "--limit", type=int, default=50, help="返回条数")
    
    # 原生模式参数
    parser.add_argument("--body-file", metavar="WORKSPACE",
                        help=f"从文件读取 ES 请求体，传入工作区路径 (文件位置: <WORKSPACE>/{BODY_DIR_NAME}/{BODY_FILE_NAME})")
    parser.add_argument("--endpoint", default="_search",
                        help="ES 端点 (默认 _search，可选 _count/_msearch 等)")
    
    # 输出控制
    parser.add_argument("--format", choices=["text", "json", "raw"], default="text",
                        help="输出格式: text(默认)/json(简化)/raw(原始ES响应)")
    
    # 工具命令
    parser.add_argument("--list-services", action="store_true", help="列出所有服务")
    parser.add_argument("--show-index", action="store_true", help="显示索引模式")
    
    args = parser.parse_args()
    
    try:
        # 列出服务
        if args.list_services:
            services = get_all_services()
            print(f"可用服务 ({len(services)} 个):\n")
            for s in services:
                print(f"  {s}")
            return
        
        # 解析索引
        if args.index:
            index = args.index
            matched_services = None
        elif args.services:
            all_services = get_all_services()
            matched_services = []
            for pattern in args.services:
                matches = match_services(pattern, all_services)
                if not matches:
                    print(f"警告: 未找到匹配 '{pattern}' 的服务", file=sys.stderr)
                else:
                    matched_services.extend(matches)
            if not matched_services:
                print("错误: 没有匹配到任何服务", file=sys.stderr)
                sys.exit(1)
            matched_services = list(set(matched_services))
            if len(matched_services) > 1:
                print(f"匹配到 {len(matched_services)} 个服务: {', '.join(matched_services)}", file=sys.stderr)
            index = build_index_pattern(matched_services)
        else:
            index = build_index_pattern(order_mode=bool(args.order))
            matched_services = None
        
        # 显示索引模式
        if args.show_index:
            print(f"索引模式: {index}")
            return
        
        # 原生模式
        if args.body_file:
            workspace = Path(args.body_file)
            body_file = workspace / BODY_DIR_NAME / BODY_FILE_NAME
            if not body_file.exists():
                print(f"错误: 请求体文件不存在", file=sys.stderr)
                print(f"请将 ES 请求体 JSON 写入: {body_file}", file=sys.stderr)
                sys.exit(1)
            with open(body_file, 'r', encoding='utf-8') as f:
                body = json.load(f)
            
            result = raw_query(index, body, args.endpoint)
            print(format_output(result, args.format if args.format != "text" else "raw"))
            return
        
        # 简化模式 - 至少需要一个条件
        if not any([args.services, args.index, args.keywords, args.order, args.level]):
            parser.print_help()
            print("\n错误: 请指定查询条件或使用 --body-file 传入原生请求体", file=sys.stderr)
            sys.exit(1)
        
        time_range = args.time_range
        if not args.start and not time_range:
            time_range = "7d" if args.order else "1h"
        
        result = simple_search(
            index=index,
            services=matched_services,
            keywords=args.keywords,
            order_sn=args.order,
            level=args.level,
            time_range=time_range if not args.start else None,
            start=args.start,
            end=args.end,
            limit=args.limit
        )
        
        print(format_output(result, args.format))
        
    except json.JSONDecodeError as e:
        print(f"JSON 解析错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
