#pragma once
#include <atomic>
#include <cstdint>
#if defined(__APPLE__)
#include <mach/mach.h>
#include <thread>
#endif

namespace vcpi {
// Construct once in shared memory before publishing the containing transport.
// No process-local Mach port names may be stored here.
struct WakeupState {
    alignas(4) std::atomic<std::uint32_t> pending{0};
#if defined(__APPLE__)
    char serviceName[128]{};
#endif
};

// Single waiter, multiple publishers. Publish work before notify(); wait() FIRST,
// then inspect/drain work. Notifications are durable but coalesce, not a count.
// Construction/destruction are control-only. The creator must outlive importers;
// stop/join every local waiter and publisher before destroying its Wakeup.
class Wakeup {
public:
    Wakeup(WakeupState& state, bool create);
    ~Wakeup();
    Wakeup(const Wakeup&) = delete;
    Wakeup& operator=(const Wakeup&) = delete;
    void notify() noexcept;
    void wait();
private:
    WakeupState& state;
#if defined(__APPLE__)
    // These names belong exclusively to this process's Mach IPC namespace.
    semaphore_t semaphore = MACH_PORT_NULL;
    mach_port_t servicePort = MACH_PORT_NULL;
    bool creator = false;
    std::atomic<bool> stopping{false};
    std::thread broker;
    void serve() noexcept;
    void cleanup() noexcept;
#endif
};
} // namespace vcpi
