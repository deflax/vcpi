#include "runtime.hpp"
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <chrono>
#include <type_traits>

namespace py = pybind11;
namespace vcpi {
namespace {
py::object pythonScalar(const Scalar& value) {
    return std::visit([](const auto& item) -> py::object { return py::cast(item); }, value);
}
Scalar nativeScalar(const py::object& value) {
    if (py::isinstance<py::bool_>(value)) return value.cast<bool>();
    if (py::isinstance<py::str>(value)) return value.cast<std::string>();
    if (PyNumber_Check(value.ptr())) return value.cast<double>();
    throw std::invalid_argument("Parameter values must be numeric, bool or string");
}
py::dict metadataDict(const PluginMetadata& info) {
    py::dict result;
    result["name"] = info.name;
    result["descriptive_name"] = info.descriptiveName;
    result["manufacturer_name"] = info.manufacturer;
    result["category"] = info.category;
    result["version"] = info.version;
    result["identifier"] = info.identifier;
    result["reported_latency_samples"] = info.latency;
    result["path_to_plugin_file"] = info.path;
    result["info_type"] = info.infoType;
    return result;
}
py::dict parameterDescriptor(const ParameterSnapshot& info) {
    py::dict descriptor;
    descriptor["range"] = py::make_tuple(info.low, info.high);
    descriptor["kind"] = info.kind;
    descriptor["label"] = info.label;
    descriptor["units"] = info.units;
    descriptor["valid_values"] = info.validValues;
    descriptor["raw_value"] = info.rawValue;
    descriptor["string_value"] = info.stringValue;
    descriptor["is_automatable"] = info.automatable;
    descriptor["is_boolean"] = info.boolean;
    descriptor["is_discrete"] = info.discrete;
    descriptor["value"] = pythonScalar(info.value);
    return descriptor;
}
py::dict parameterDict(const std::vector<ParameterSnapshot>& parameters) {
    py::dict result;
    for (const auto& info : parameters)
        result[py::str(info.key)] = parameterDescriptor(info);
    return result;
}
py::dict deckDict(const DeckStatus& info) {
    py::dict result;
    result["bank_id"] = info.bankId;
    result["track_id"] = info.trackId;
    result["track_title"] = info.trackTitle;
    result["track_select"] = info.trackSelect;
    result["requested_track_id"] = info.requestedTrackId;
    result["requested_track_select"] = info.requestedTrackSelect;
    result["request_id"] = info.requestId;
    result["state"] = info.state;
    result["loading"] = info.loading;
    result["ready"] = info.ready;
    result["buffering"] = info.buffering;
    result["ended"] = info.ended;
    result["play"] = info.play;
    result["position"] = info.position;
    result["duration"] = info.duration;
    result["cue"] = info.cue;
    result["pitch"] = info.pitch;
    result["nudge"] = info.nudge;
    result["phase_offset"] = info.phaseOffset;
    result["bpm"] = info.bpm;
    result["beat_offset"] = info.beatOffset;
    result["beat_confidence"] = info.beatConfidence;
    result["sync"] = info.sync;
    result["sync_active"] = info.syncActive;
    result["sync_error"] = info.syncError;
    result["playback_speed"] = info.playbackSpeed;
    result["beat_phase_error"] = info.beatPhaseError;
    result["error"] = info.error;
    return result;
}
py::dict realtimeDict(const RealtimeStats& info) {
    py::dict result;
    result["state"] = info.state;
    result["policy"] = info.policy;
    result["priority"] = info.priority;
    result["requested_priority"] = info.requestedPriority;
    result["requested_policy"] = info.requestedPolicy;
    result["error_domain"] = info.errorDomain;
    result["error_message"] = info.errorMessage;
    result["error"] = info.error;
    result["thread_id"] = info.threadId;
    return result;
}
py::dict transportDict(const TransportStats& info) {
    py::dict result;
    result["sequence"] = info.sequence;
    result["submitted"] = info.submitted;
    result["progress"] = info.progress;
    result["misses"] = info.misses;
    result["failure"] = info.failure;
    result["midi_pending"] = info.midiPending;
    result["midi_sent"] = info.midiSent;
    result["midi_consumed"] = info.midiConsumed;
    result["audio_peak"] = info.audioPeak;
    result["realtime"] = realtimeDict(info.realtime);
    return result;
}
py::dict engineDict(const EngineStats& info) {
    py::dict result;
    result["running"] = info.running;
    result["eligible_tracks"] = info.eligibleTracks;
    result["underruns"] = info.underruns;
    result["render_errors"] = info.renderErrors;
    result["callback_count"] = info.callbackCount;
    result["last_callback_ms"] = info.lastCallbackMs;
    result["peak_callback_ms"] = info.peakCallbackMs;
    result["deadline_misses"] = info.deadlineMisses;
    result["block_period_ms"] = info.blockPeriodMs;
    result["backend_label"] = info.backendLabel;
    result["output_name"] = info.outputName.empty() ? py::object(py::none()) : py::object(py::str(info.outputName));
    result["output_index"] = info.outputIndex < 0 ? py::object(py::none()) : py::object(py::int_(info.outputIndex));
    result["output_peak"] = info.outputPeak;
    result["realtime"] = realtimeDict(info.realtime);
    return result;
}
py::array_t<float> audioArray(std::vector<float> samples) {
    auto* storage = new std::vector<float>(std::move(samples));
    const auto frames = static_cast<py::ssize_t>(storage->size() / 2);
    py::capsule owner(storage, [](void* pointer) { delete static_cast<std::vector<float>*>(pointer); });
    return py::array_t<float>({frames, static_cast<py::ssize_t>(2)},
                             {static_cast<py::ssize_t>(2 * sizeof(float)), static_cast<py::ssize_t>(sizeof(float))},
                             storage->data(), owner);
}
}
PYBIND11_MODULE(_vcpi_native, module) {
    module.doc() = "Native vcpi control API; audio rendering never calls Python";
    py::register_exception<MidiFull>(module, "FifoFull", PyExc_BufferError);
    py::register_exception<NativeTimeout>(module, "NativeTimeout", PyExc_TimeoutError);
    py::register_exception<NativeOwnershipFailure>(module, "OwnershipError", PyExc_RuntimeError);
    py::class_<Plugin, std::shared_ptr<Plugin>>(module, "Plugin")
        .def(py::init<const std::string&, double, int, const std::string&>(),
             py::arg("path"), py::arg("sample_rate"), py::arg("frames"), py::arg("kind") = "instrument",
             py::call_guard<py::gil_scoped_release>())
        .def("metadata", [](Plugin& plugin) {
            PluginMetadata result;
            { py::gil_scoped_release release; result = plugin.metadata(); }
            return metadataDict(result);
        })
        .def("parameters", [](Plugin& plugin) {
            std::vector<ParameterSnapshot> result;
            { py::gil_scoped_release release; result = plugin.parameters(); }
            return parameterDict(result);
        })
        .def("parameter", [](Plugin& plugin, const std::string& key) {
            ParameterSnapshot result;
            { py::gil_scoped_release release; result = plugin.parameterSnapshot(key); }
            return parameterDescriptor(result);
        }, py::arg("name"))
        .def("get_parameter", [](Plugin& plugin, const std::string& key) {
            Scalar result;
            { py::gil_scoped_release release; result = plugin.getParameter(key); }
            return pythonScalar(result);
        }, py::arg("name"))
        .def("set_parameter", [](Plugin& plugin, const std::string& key, const py::object& value) {
            const auto input = nativeScalar(value);
            Scalar result;
            { py::gil_scoped_release release; result = plugin.setParameter(key, input); }
            return pythonScalar(result);
        }, py::arg("name"), py::arg("value"))
        .def("deck_status", [](Plugin& plugin) {
            DeckStatus result;
            { py::gil_scoped_release release; result = plugin.deckStatus(); }
            return deckDict(result);
        })
        .def("deck_command", &Plugin::deckCommand, py::arg("action"), py::arg("value") = 0.0,
             py::call_guard<py::gil_scoped_release>())
        .def("set_deck_clock",
             [monotonic = py::object(py::module_::import("time").attr("monotonic"))](
                 Plugin& plugin, double bpm, double beat, double sampledAt) {
                 const double pythonNow = monotonic().cast<double>();
                 const double nativeNow = std::chrono::duration<double>(
                     std::chrono::steady_clock::now().time_since_epoch()).count();
                 const double nativeSampledAt = nativeNow - (pythonNow - sampledAt);
                 py::gil_scoped_release release;
                 plugin.setDeckClock(bpm, beat, nativeSampledAt);
             }, py::arg("bpm"), py::arg("beat"), py::arg("sampled_at"))
        .def("update_deck_catalog", &Plugin::updateDeckCatalog, py::arg("catalog"),
             py::call_guard<py::gil_scoped_release>())
        .def("load_preset", &Plugin::loadPreset, py::arg("path"), py::call_guard<py::gil_scoped_release>())
        .def("get_state", [](Plugin& plugin) {
            juce::MemoryBlock result;
            { py::gil_scoped_release release; result = plugin.getState(); }
            return py::bytes(static_cast<const char*>(result.getData()), result.getSize());
        })
        .def("load_state", [](Plugin& plugin, const py::bytes& state) {
            const std::string data = state;
            py::gil_scoped_release release;
            plugin.loadState(data);
        }, py::arg("state"))
        .def("load_saved_state", [](Plugin& plugin, const py::bytes& state) {
            const std::string data = state;
            py::gil_scoped_release release;
            plugin.loadSavedState(data);
        }, py::arg("state"));
    py::class_<Transport, std::shared_ptr<Transport>>(module, "Transport")
        .def(py::init<const std::string&, int, bool>(), py::arg("path"), py::arg("frames"), py::arg("create"), py::call_guard<py::gil_scoped_release>())
        .def("send_midi", [](Transport& transport, const py::bytes& bytes) {
            const std::string message = bytes;
            py::gil_scoped_release release;
            transport.sendMidi(message);
        }, py::arg("message"))
        .def("stats", [](Transport& transport) {
            TransportStats result;
            { py::gil_scoped_release release; result = transport.stats(); }
            return transportDict(result);
        });
    py::class_<TrackRunner, std::shared_ptr<TrackRunner>>(module, "TrackRunner")
        .def(py::init<std::shared_ptr<Transport>, std::shared_ptr<Plugin>, double, int>(),
             py::arg("transport"), py::arg("plugin"), py::arg("sample_rate"), py::arg("frames"), py::call_guard<py::gil_scoped_release>())
        .def("set_effects", &TrackRunner::setEffects, py::arg("effects"), py::call_guard<py::gil_scoped_release>())
        .def("barrier", &TrackRunner::barrier, py::call_guard<py::gil_scoped_release>())
        .def("shutdown", &TrackRunner::shutdown, py::call_guard<py::gil_scoped_release>())
        .def("render_once", [](TrackRunner& runner) {
            std::vector<float> result;
            { py::gil_scoped_release release; result = runner.renderOnce(); }
            return audioArray(std::move(result));
        });
    py::class_<Engine, std::shared_ptr<Engine>>(module, "Engine")
        .def(py::init<double, int>(), py::arg("sample_rate"), py::arg("frames"), py::call_guard<py::gil_scoped_release>())
        .def("attach", &Engine::attach, py::arg("index"), py::arg("transport"), py::arg("gain") = 1.0, py::arg("muted") = false, py::arg("solo") = false, py::call_guard<py::gil_scoped_release>())
        .def("set_track", &Engine::setTrack, py::arg("index"), py::arg("gain"), py::arg("muted"), py::arg("solo"), py::call_guard<py::gil_scoped_release>())
        .def("set_master_gain", &Engine::setMasterGain, py::arg("gain"), py::call_guard<py::gil_scoped_release>())
        .def("set_mixer", &Engine::setMixer, py::arg("tracks"), py::arg("master_gain"), py::call_guard<py::gil_scoped_release>())
        .def("set_master_effects", &Engine::setMasterEffects, py::arg("effects"), py::call_guard<py::gil_scoped_release>())
        .def("start", [](Engine& engine, const py::object& device) {
            int index = -1;
            std::string name;
            if (py::isinstance<py::str>(device)) name = device.cast<std::string>();
            else if (!device.is_none()) index = device.cast<int>();
            py::gil_scoped_release release;
            engine.start(index, name);
        }, py::arg("device") = py::none())
        .def("stop", &Engine::stop, py::call_guard<py::gil_scoped_release>())
        .def("shutdown", &Engine::shutdown, py::call_guard<py::gil_scoped_release>())
        .def("barrier", &Engine::barrier, py::call_guard<py::gil_scoped_release>())
        .def("stats", [](Engine& engine) {
            EngineStats result;
            { py::gil_scoped_release release; result = engine.stats(); }
            return engineDict(result);
        })
        .def("render_block", [](Engine& engine) {
            std::vector<float> result;
            { py::gil_scoped_release release; result = engine.renderBlock(); }
            return audioArray(std::move(result));
        });
    module.def("devices", [] {
        std::vector<DeviceInfo> result;
        { py::gil_scoped_release release; result = devices(); }
        py::list values;
        for (const auto& info : result) {
            py::dict item;
            item["name"] = info.name;
            item["max_input_channels"] = info.maxInputChannels;
            item["max_output_channels"] = info.maxOutputChannels;
            item["hostapi"] = info.hostapi;
            item["index"] = info.index;
            values.append(std::move(item));
        }
        return values;
    });
    module.def("hostapis", [] {
        std::vector<std::string> result;
        { py::gil_scoped_release release; result = hostapis(); }
        py::list values;
        for (const auto& name : result) { py::dict item; item["name"] = name; values.append(std::move(item)); }
        return values;
    });
    module.def("backend_info", [] {
        py::dict result;
        result["engine"] = "native C++17 / PortAudio";
        result["plugin_host"] = "JUCE 8.0.6 VST3";
        result["transport_version"] = transportVersion;
        return result;
    });
}
} // namespace vcpi
