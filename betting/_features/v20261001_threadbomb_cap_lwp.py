import os, sys, concurrent.futures, concurrent.futures.thread, asyncio, atexit, importlib
os.environ.setdefault("BG_THREADPOOL_MAX", "4")
os.environ.setdefault("ASGI_SYNC_MAX", "4")
os.environ.setdefault("UVICORN_ASGI_THREADS", "4")
os.environ.setdefault("ASGI_THREADS", "4")
os.environ.setdefault("ASGI_THREAD_POOL_SIZE", "4")
os.environ.setdefault("DJANGO_ASGI_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("PYTHONASYNCIODEBUG", "0")
_HARD_MAX = 4
_SHARED_ASGI_EXEC_V10B = None
_PATCHED_FLAG_V10B = False
from concurrent.futures import ThreadPoolExecutor as _TPE_orig_class
_TRUE_INIT_SAVED_V10B = _TPE_orig_class.__init__
def _V10B_TPE_INIT_CAP_HARD_4_MAX(self, *args, **kwargs):
    try:
        requested = kwargs.get("max_workers", None)
        try:
            requested_int = int(requested) if requested is not None else None
        except Exception:
            requested_int = None
        effective = _HARD_MAX
        if requested_int is not None and requested_int > 0 and requested_int < effective:
            effective = requested_int
        kwargs["max_workers"] = effective
        if "thread_name_prefix" not in kwargs:
            kwargs["thread_name_prefix"] = "V10B-"
    except Exception:
        kwargs["max_workers"] = _HARD_MAX
        if "thread_name_prefix" not in kwargs:
            kwargs["thread_name_prefix"] = "V10B-"
    return _TRUE_INIT_SAVED_V10B(self, *args, **kwargs)
concurrent.futures.ThreadPoolExecutor.__init__ = _V10B_TPE_INIT_CAP_HARD_4_MAX
concurrent.futures.thread.ThreadPoolExecutor.__init__ = _V10B_TPE_INIT_CAP_HARD_4_MAX
try:
    import concurrent.futures._base
except Exception:
    pass
try:
    _orig_os_cpu_count = os.cpu_count
    def _v10b_os_cpu_count_always_4():
        try:
            return 4
        except Exception:
            return 4
    os.cpu_count = _v10b_os_cpu_count_always_4
except Exception:
    pass
def _v10b_get_shared_asgi_exec():
    global _SHARED_ASGI_EXEC_V10B
    if _SHARED_ASGI_EXEC_V10B is None or getattr(_SHARED_ASGI_EXEC_V10B, "_shutdown", False):
        try:
            _SHARED_ASGI_EXEC_V10B = _TPE_orig_class(max_workers=4, thread_name_prefix="V10BASGI-")
        except Exception:
            _SHARED_ASGI_EXEC_V10B = None
    return _SHARED_ASGI_EXEC_V10B
def _v10b_event_loop_set_default_asgi_shared(*_ign):
    try:
        exe = _v10b_get_shared_asgi_exec()
        if exe is None:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.set_default_executor(exe)
        except Exception:
            try:
                loop = asyncio.new_event_loop()
                loop.set_default_executor(exe)
                asyncio.set_event_loop(loop)
            except Exception:
                pass
    except Exception:
        pass
def apply_all_patches():
    global _PATCHED_FLAG_V10B
    if _PATCHED_FLAG_V10B:
        return
    _PATCHED_FLAG_V10B = True
    try:
        _v10b_get_shared_asgi_exec()
        import sys as _sys_v10b
        _sys_v10b.modules.setdefault("__v10b_shared_asgi_exec__", _SHARED_ASGI_EXEC_V10B)
    except Exception:
        pass
    try:
        from asgiref.sync import SyncToAsync as _STA_v10b
        def _v10b_sta_tp_prop(_self):
            return _v10b_get_shared_asgi_exec()
        _STA_v10b.thread_pool = property(_v10b_sta_tp_prop)
        _old_sta_init_v10b = _STA_v10b.__init__
        def _v10b_sta_init_singleton(self, *a, **kw):
            result_old = _old_sta_init_v10b(self, *a, **kw)
            exe = _v10b_get_shared_asgi_exec()
            try:
                self.thread_pool_executor = exe
            except Exception:
                pass
            try:
                object.__setattr__(self, "thread_pool_executor", exe)
            except Exception:
                pass
            return result_old
        _STA_v10b.__init__ = _v10b_sta_init_singleton
    except Exception:
        pass
    try:
        _v10b_event_loop_set_default_asgi_shared()
    except Exception:
        pass
    try:
        os.register_at_fork(after_in_child=_v10b_event_loop_set_default_asgi_shared)
    except Exception:
        try:
            atexit.register(_v10b_event_loop_set_default_asgi_shared)
        except Exception:
            pass
