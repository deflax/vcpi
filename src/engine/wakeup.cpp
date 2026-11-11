#include "wakeup.hpp"
#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <string>
#if defined(__linux__)
#include <linux/futex.h>
#include <sys/syscall.h>
#include <unistd.h>
#elif defined(__APPLE__)
#include <cstdio>
#include <mach/mach_error.h>
#include <mach/semaphore.h>
#include <mach/sync_policy.h>
#include <servers/bootstrap.h>
#include <uuid/uuid.h>
#else
#error "Wakeup requires Linux futexes or macOS Mach semaphores"
#endif

namespace vcpi {
static_assert(std::atomic<std::uint32_t>::is_always_lock_free,
              "Cross-process wakeup atomic must be lock free");
static_assert(sizeof(std::atomic<std::uint32_t>) == sizeof(std::uint32_t),
              "Futex word must be exactly 32 bits");

#if defined(__linux__)
Wakeup::Wakeup(WakeupState& shared, bool) : state(shared) {}
Wakeup::~Wakeup() = default;

void Wakeup::notify() noexcept {
    if (state.pending.exchange(1, std::memory_order_acq_rel) == 0) {
        // Not FUTEX_PRIVATE: the waiter has another mapping in another process.
        if (::syscall(SYS_futex, &state.pending, FUTEX_WAKE, 1, nullptr, nullptr, 0) < 0)
            __builtin_trap(); // Broken wakeup cannot safely be ignored in an audio callback.
    }
}

void Wakeup::wait() {
    for (;;) {
        if (state.pending.exchange(0, std::memory_order_acq_rel) != 0) return;
        // A notification between exchange and syscall changes the word to 1:
        // FUTEX_WAIT then returns EAGAIN instead of sleeping past that signal.
        if (::syscall(SYS_futex, &state.pending, FUTEX_WAIT, 0, nullptr, nullptr, 0) == 0)
            continue;
        const auto error = errno;
        if (error == EINTR || error == EAGAIN) continue;
        throw std::runtime_error(std::string("wait for audio work: ") + std::strerror(error));
    }
}
#elif defined(__APPLE__)
namespace {
constexpr mach_msg_id_t attachRequest = 0x56435049;
constexpr mach_msg_id_t attachReply = attachRequest + 1;
constexpr mach_msg_timeout_t attachTimeoutMs = 5000;

struct SemaphoreReply {
    mach_msg_header_t header{};
    mach_msg_body_t body{};
    mach_msg_port_descriptor_t port{};
};
struct ReplyBuffer {
    SemaphoreReply message;
    mach_msg_max_trailer_t trailer{};
};
struct RequestBuffer {
    mach_msg_header_t header{};
    mach_msg_max_trailer_t trailer{};
};

std::runtime_error machError(const char* operation, kern_return_t error) {
    return std::runtime_error(std::string(operation) + ": " + mach_error_string(error)
                              + " (" + std::to_string(error) + ")");
}

// Only used during setup, never by notify()/wait().
struct ScopedPort {
    mach_port_t name = MACH_PORT_NULL;
    bool receive;
    explicit ScopedPort(bool receiveRight) : receive(receiveRight) {}
    ~ScopedPort() {
        if (name == MACH_PORT_NULL) return;
        if (receive)
            (void) ::mach_port_mod_refs(mach_task_self(), name, MACH_PORT_RIGHT_RECEIVE, -1);
        else (void) ::mach_port_deallocate(mach_task_self(), name);
    }
};

// bootstrap_register is deprecated but remains the public SDK API for a
// dynamically named endpoint. Sandboxes/isolated bootstrap domains may deny
// registration or lookup: construction fails explicitly, without polling.
// Register a *receive endpoint*, not the semaphore itself: destroying this
// endpoint unregisters the binding through launchd's dead-name notification.
// A directly registered semaphore would leave launchd holding a live send right
// even after semaphore_destroy, preventing reliable service cleanup.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
kern_return_t registerService(char* name, mach_port_t port) {
    return ::bootstrap_register(bootstrap_port, name, port);
}
#pragma clang diagnostic pop

semaphore_t importSemaphore(WakeupState& state) {
    if (state.serviceName[0] == '\0'
        || std::memchr(state.serviceName, '\0', sizeof(state.serviceName)) == nullptr)
        throw std::runtime_error("Invalid audio wakeup rendezvous name");
    ScopedPort service(false);
    auto result = ::bootstrap_look_up(bootstrap_port, state.serviceName, &service.name);
    if (result != KERN_SUCCESS)
        throw machError("look up audio wakeup service (requires a shared bootstrap namespace)", result);
    ScopedPort reply(true);
    result = ::mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &reply.name);
    if (result != KERN_SUCCESS) throw machError("allocate audio wakeup reply port", result);

    mach_msg_header_t request{};
    request.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND_ONCE);
    request.msgh_size = sizeof(request);
    request.msgh_remote_port = service.name;
    request.msgh_local_port = reply.name;
    request.msgh_id = attachRequest;
    result = ::mach_msg(&request, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(request), 0,
                        MACH_PORT_NULL, attachTimeoutMs, MACH_PORT_NULL);
    if (result != MACH_MSG_SUCCESS) {
        ::mach_msg_destroy(&request);
        throw machError("request audio wakeup semaphore", result);
    }
    ReplyBuffer response{};
    result = ::mach_msg(&response.message.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                        sizeof(response), reply.name, attachTimeoutMs, MACH_PORT_NULL);
    if (result != MACH_MSG_SUCCESS) throw machError("receive audio wakeup semaphore", result);
    const auto& message = response.message;
    if (message.header.msgh_id != attachReply
        || message.header.msgh_size != sizeof(SemaphoreReply)
        || !(message.header.msgh_bits & MACH_MSGH_BITS_COMPLEX)
        || message.header.msgh_remote_port != MACH_PORT_NULL
        || message.body.msgh_descriptor_count != 1
        || message.port.type != MACH_MSG_PORT_DESCRIPTOR
        || message.port.disposition != MACH_MSG_TYPE_PORT_SEND
        || !MACH_PORT_VALID(message.port.name)) {
        ::mach_msg_destroy(&response.message.header);
        throw std::runtime_error("Invalid audio wakeup semaphore reply");
    }
    // The descriptor is an actual transferred send right in THIS task, not the
    // integer port name that happened to identify it in the creator task.
    return message.port.name;
}
} // namespace

Wakeup::Wakeup(WakeupState& shared, bool create) : state(shared), creator(create) {
    static_assert(sizeof(state.serviceName) == BOOTSTRAP_MAX_NAME_LEN,
                  "Wakeup service name must fit bootstrap name_t");
    if (!creator) {
        semaphore = importSemaphore(state);
        return;
    }
    try {
        auto result = ::semaphore_create(mach_task_self(), &semaphore, SYNC_POLICY_FIFO, 0);
        if (result != KERN_SUCCESS) throw machError("create audio wakeup semaphore", result);
        result = ::mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &servicePort);
        if (result != KERN_SUCCESS) throw machError("allocate audio wakeup service", result);
        result = ::mach_port_insert_right(mach_task_self(), servicePort, servicePort,
                                          MACH_MSG_TYPE_MAKE_SEND);
        if (result != KERN_SUCCESS) throw machError("create audio wakeup service send right", result);
        uuid_t id;
        char text[37];
        ::uuid_generate_random(id);
        ::uuid_unparse_lower(id, text);
        std::snprintf(state.serviceName, sizeof(state.serviceName), "org.vcpi.wakeup.%s", text);
        result = registerService(state.serviceName, servicePort);
        if (result != KERN_SUCCESS)
            throw machError("register audio wakeup service (requires bootstrap registration permission)", result);
        broker = std::thread([this] { serve(); });
    } catch (...) {
        cleanup();
        throw;
    }
}

Wakeup::~Wakeup() { cleanup(); }

void Wakeup::cleanup() noexcept {
    stopping.store(true, std::memory_order_release);
    // Drop our send reference while the receive right still pins the name,
    // then remove the receive right to interrupt the broker and unregister
    // the bootstrap endpoint. The send decrement can fail during partial
    // construction, before mach_port_insert_right has created that reference.
    if (servicePort != MACH_PORT_NULL) {
        (void) ::mach_port_mod_refs(mach_task_self(), servicePort, MACH_PORT_RIGHT_SEND, -1);
        (void) ::mach_port_mod_refs(mach_task_self(), servicePort, MACH_PORT_RIGHT_RECEIVE, -1);
    }
    if (broker.joinable()) broker.join();
    servicePort = MACH_PORT_NULL;
    if (semaphore != MACH_PORT_NULL) {
        if (creator) {
            // task.defs declares a MOVE_SEND argument: this consumes our right.
            (void) ::semaphore_destroy(mach_task_self(), semaphore);
        } else {
            (void) ::mach_port_deallocate(mach_task_self(), semaphore);
        }
        semaphore = MACH_PORT_NULL;
    }
}

void Wakeup::serve() noexcept {
    while (!stopping.load(std::memory_order_acquire)) {
        RequestBuffer request{};
        const auto result = ::mach_msg(&request.header, MACH_RCV_MSG, 0, sizeof(request),
                                       servicePort, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL);
        if (result != MACH_MSG_SUCCESS) {
            if (stopping.load(std::memory_order_acquire)) return;
            // Without MACH_RCV_LARGE an oversized request is discarded.
            if (result == MACH_RCV_INTERRUPTED || result == MACH_RCV_TOO_LARGE) continue;
            __builtin_trap();
        }
        if (request.header.msgh_id != attachRequest
            || request.header.msgh_size != sizeof(mach_msg_header_t)
            || (request.header.msgh_bits & MACH_MSGH_BITS_COMPLEX)
            || MACH_MSGH_BITS_REMOTE(request.header.msgh_bits) != MACH_MSG_TYPE_PORT_SEND_ONCE
            || !MACH_PORT_VALID(request.header.msgh_remote_port)) {
            ::mach_msg_destroy(&request.header);
            continue;
        }
        SemaphoreReply reply{};
        reply.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_MOVE_SEND_ONCE, 0)
            | MACH_MSGH_BITS_COMPLEX;
        reply.header.msgh_size = sizeof(reply);
        reply.header.msgh_remote_port = request.header.msgh_remote_port;
        reply.header.msgh_id = attachReply;
        reply.body.msgh_descriptor_count = 1;
        reply.port.name = semaphore;
        reply.port.disposition = MACH_MSG_TYPE_COPY_SEND;
        reply.port.type = MACH_MSG_PORT_DESCRIPTOR;
        const auto sent = ::mach_msg(&reply.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                                     sizeof(reply), 0, MACH_PORT_NULL, 0, MACH_PORT_NULL);
        // Reclaim the reply right/copy if the attaching process disappeared.
        if (sent != MACH_MSG_SUCCESS) ::mach_msg_destroy(&reply.header);
    }
}

void Wakeup::notify() noexcept {
    if (state.pending.exchange(1, std::memory_order_acq_rel) == 0) {
        // semaphore_signal is a nonblocking kernel trap, not a Mach message send.
        if (::semaphore_signal(semaphore) != KERN_SUCCESS) __builtin_trap();
    }
}

void Wakeup::wait() {
    for (;;) {
        const auto result = ::semaphore_wait(semaphore);
        if (result == KERN_ABORTED) continue;
        if (result != KERN_SUCCESS) throw machError("wait for audio work", result);
        // ALWAYS consume the semaphore token before clearing pending, even if a
        // notify preceded wait(). Clearing first can accumulate stale tokens.
        // A publisher before this exchange joins this batch; one after it emits
        // the next token. Thus at most one token is outstanding, without losses.
        state.pending.exchange(0, std::memory_order_acq_rel);
        return;
    }
}
#endif
} // namespace vcpi
