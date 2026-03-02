"""下载与解压相关异常定义."""

from dataclasses import dataclass


class DownloadError(Exception):
    """下载流程异常."""

    pass


class DecompressError(Exception):
    """解压流程异常."""

    pass


@dataclass
class BundleJobFailure:
    """单个 bundle 任务失败信息."""

    bundle_id: int
    error: Exception


class DownloadBatchError(DownloadError):
    """批量下载存在失败任务时抛出的异常。."""

    def __init__(self, failures: list[BundleJobFailure]):
        """初始化批量下载异常。.

        Args:
            failures: 失败的 bundle 任务列表。
        """
        self.failures = failures
        summary = ", ".join(f"{failure.bundle_id:016X}:{failure.error}" for failure in failures[:5])
        if len(failures) > 5:
            summary = f"{summary}, ... total={len(failures)}"
        super().__init__(f"存在 {len(failures)} 个 bundle 任务失败: {summary}")
