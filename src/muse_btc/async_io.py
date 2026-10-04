"""Keep synchronous persistence/analysis off the event loop, including during shutdown."""

import asyncio


async def run_sync(function, /, *args, **kwargs):
    # Cancellation cannot stop a Python thread. Join it before the caller releases
    # its collection lease or closes resources; shield also survives repeated cancels.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            if not cancelled:
                raise
            break
    if cancelled:
        # Retrieve exceptions to avoid abandoned-task warnings, then honor cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()
