import psutil
import time
import json
import requests
from typing import Dict, Any
from dataclasses import dataclass, asdict


@dataclass
class MemoryStats:
    timestamp: float
    process_memory_mb: float
    process_memory_percent: float
    system_memory_mb: float
    system_memory_percent: float
    pending_futures_count: int
    registry_stats: Dict[str, Any]


class MemoryMonitor:

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.process = psutil.Process()
    
    def get_memory_stats(self) -> MemoryStats:
        process_memory = self.process.memory_info()
        process_memory_mb = process_memory.rss / 1024 / 1024
        process_memory_percent = self.process.memory_percent()
        
        system_memory = psutil.virtual_memory()
        system_memory_mb = system_memory.used / 1024 / 1024
        system_memory_percent = system_memory.percent
        
        try:
            response = requests.get(f"{self.base_url}/_orchestrator/stats", timeout=5)
            registry_stats = response.json().get("data", {})
            pending_futures_count = registry_stats.get("current_pending", 0)
        except Exception as e:
            print(f"Failed to get registry stats: {e}")
            registry_stats = {}
            pending_futures_count = 0
        
        return MemoryStats(
            timestamp=time.time(),
            process_memory_mb=process_memory_mb,
            process_memory_percent=process_memory_percent,
            system_memory_mb=system_memory_mb,
            system_memory_percent=system_memory_percent,
            pending_futures_count=pending_futures_count,
            registry_stats=registry_stats
        )
    
    def monitor_loop(self, interval: float = 60.0, duration: float = None):
        """监控循环"""
        start_time = time.time()
        stats_history = []
        
        print(f"Starting memory monitoring (interval: {interval}s)")
        print("=" * 80)
        
        try:
            while True:
                if duration and (time.time() - start_time) > duration:
                    break
                
                stats = self.get_memory_stats()
                stats_history.append(stats)

                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats.timestamp))}]")
                print(f"Process Memory: {stats.process_memory_mb:.2f} MB ({stats.process_memory_percent:.1f}%)")
                print(f"System Memory: {stats.system_memory_mb:.2f} MB ({stats.system_memory_percent:.1f}%)")
                print(f"Pending Futures: {stats.pending_futures_count}")
                print(f"Registry Stats: {json.dumps(stats.registry_stats, indent=2)}")
                print("-" * 80)
                
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print("\nMonitoring stopped by user")
        
        self.generate_report(stats_history)
    
    def generate_report(self, stats_history: list[MemoryStats]):
        """生成监控报告"""
        if not stats_history:
            return
        
        print("\n" + "=" * 80)
        print("MEMORY MONITORING REPORT")
        print("=" * 80)
        
        # 计算统计信息
        process_memory_values = [s.process_memory_mb for s in stats_history]
        pending_futures_values = [s.pending_futures_count for s in stats_history]
        
        print(f"Monitoring Duration: {stats_history[-1].timestamp - stats_history[0].timestamp:.1f} seconds")
        print(f"Sample Count: {len(stats_history)}")
        print()
        
        print("Process Memory (MB):")
        print(f"  Min: {min(process_memory_values):.2f}")
        print(f"  Max: {max(process_memory_values):.2f}")
        print(f"  Avg: {sum(process_memory_values) / len(process_memory_values):.2f}")
        print()
        
        print("Pending Futures:")
        print(f"  Min: {min(pending_futures_values)}")
        print(f"  Max: {max(pending_futures_values)}")
        print(f"  Avg: {sum(pending_futures_values) / len(pending_futures_values):.1f}")
        print()
        
        if len(stats_history) > 10:
            first_half = process_memory_values[:len(process_memory_values)//2]
            second_half = process_memory_values[len(process_memory_values)//2:]
            
            first_avg = sum(first_half) / len(first_half)
            second_avg = sum(second_half) / len(second_half)
            
            if second_avg > first_avg * 1.2:  # 增长超过20%
                print("⚠️  WARNING: Potential memory leak detected!")
                print(f"   First half average: {first_avg:.2f} MB")
                print(f"   Second half average: {second_avg:.2f} MB")
                print(f"   Growth: {((second_avg - first_avg) / first_avg * 100):.1f}%")
            else:
                print("✅ No significant memory growth detected")
        
        report_data = {
            "summary": {
                "duration_seconds": stats_history[-1].timestamp - stats_history[0].timestamp,
                "sample_count": len(stats_history),
                "process_memory": {
                    "min": min(process_memory_values),
                    "max": max(process_memory_values),
                    "avg": sum(process_memory_values) / len(process_memory_values)
                },
                "pending_futures": {
                    "min": min(pending_futures_values),
                    "max": max(pending_futures_values),
                    "avg": sum(pending_futures_values) / len(pending_futures_values)
                }
            },
            "samples": [asdict(s) for s in stats_history]
        }
        
        with open("memory_monitor_report.json", "w") as f:
            json.dump(report_data, f, indent=2)
        
        print(f"\nDetailed report saved to: memory_monitor_report.json")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Memory Monitor for Async Queue")
    parser.add_argument("--interval", type=float, default=60.0, 
                       help="Monitoring interval in seconds (default: 60)")
    parser.add_argument("--duration", type=float, default=None,
                       help="Total monitoring duration in seconds (default: infinite)")
    parser.add_argument("--url", type=str, default="http://localhost:8000",
                       help="Base URL of the application (default: http://localhost:8000)")
    
    args = parser.parse_args()
    
    monitor = MemoryMonitor(args.url)
    monitor.monitor_loop(interval=args.interval, duration=args.duration)


if __name__ == "__main__":
    main() 