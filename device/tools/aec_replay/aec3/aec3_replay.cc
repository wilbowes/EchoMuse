// aec3_replay: WebRTC AEC3 over the mic.wav/ref.wav pair aec_replay writes,
// for comparison against speex on identical input. Starts cold.
//
//   aec3_replay <dir>   ->  <dir>/aec3_full.wav    AEC3 incl. its suppressor
//                           <dir>/aec3_linear.wav  its linear filter alone
//
// 16kHz mono, 10ms blocks. Stream delay 0: the ch8 reference arrives in the
// same TDM frame as the mics (+33 samples), so there is no delay to report.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <array>
#include "api/scoped_refptr.h"
#include "api/audio/audio_processing.h"
#include "api/audio/echo_canceller3_config.h"
#include "api/audio/echo_control.h"
#include "modules/audio_processing/aec3/echo_canceller3.h"

// APM builds AEC3 with a default EchoCanceller3Config, whose
// filter.export_linear_aec_output is false, while its own
// export_linear_aec_output allocates a buffer for it: AEC3 then has no
// framer for the buffer it is handed and crashes. Constructing AEC3 here with
// both set is the only way to get the linear output (and the config hook is
// where AEC3 tuning goes).
class Aec3Factory : public webrtc::EchoControlFactory {
 public:
  explicit Aec3Factory(webrtc::EchoCanceller3Config c) : cfg_(c) {}
  std::unique_ptr<webrtc::EchoControl> Create(int rate, int nr, int nc) override {
    return std::make_unique<webrtc::EchoCanceller3>(cfg_, std::nullopt, rate, nr, nc);
  }
 private:
  webrtc::EchoCanceller3Config cfg_;
};

static std::vector<int16_t> readWav(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  std::vector<char> b((std::istreambuf_iterator<char>(f)), {});
  std::vector<int16_t> s((b.size() - 44) / 2);
  std::memcpy(s.data(), b.data() + 44, s.size() * 2);
  return s;
}

static void writeWav(const std::string& p, const std::vector<int16_t>& s) {
  std::ofstream f(p, std::ios::binary);
  uint32_t n = s.size() * 2, r = 16000, br = 32000, v32;
  uint16_t v16;
  f.write("RIFF", 4); v32 = 36 + n; f.write((char*)&v32, 4);
  f.write("WAVEfmt ", 8); v32 = 16; f.write((char*)&v32, 4);
  v16 = 1; f.write((char*)&v16, 2); f.write((char*)&v16, 2);
  f.write((char*)&r, 4); f.write((char*)&br, 4);
  v16 = 2; f.write((char*)&v16, 2); v16 = 16; f.write((char*)&v16, 2);
  f.write("data", 4); f.write((char*)&n, 4);
  f.write((const char*)s.data(), n);
}

int main(int argc, char** argv) {
  if (argc != 2) { std::cerr << "usage: aec3_replay <dir>\n"; return 2; }
  std::string d = argv[1];
  auto mic = readWav(d + "/mic.wav"), ref = readWav(d + "/ref.wav");
  size_t n = std::min(mic.size(), ref.size()) / 160;

  webrtc::AudioProcessing::Config cfg;
  cfg.echo_canceller.enabled = true;
  cfg.echo_canceller.export_linear_aec_output = true;
  // SetConfig, not ApplyConfig afterwards: the linear output buffer is only
  // allocated if export_linear_aec_output is set at creation.
  webrtc::EchoCanceller3Config ec;
  ec.filter.export_linear_aec_output = true;
  rtc::scoped_refptr<webrtc::AudioProcessing> apm =
      webrtc::AudioProcessingBuilder()
          .SetConfig(cfg)
          .SetEchoControlFactory(std::make_unique<Aec3Factory>(ec))
          .Create();
  webrtc::StreamConfig sc(16000, 1);

  std::vector<int16_t> full(n * 160), lin(n * 160);
  std::array<float, 160> lbuf;
  for (size_t i = 0; i < n; i++) {
    int16_t r[160], m[160];
    std::memcpy(r, &ref[i * 160], sizeof r);
    std::memcpy(m, &mic[i * 160], sizeof m);
    apm->ProcessReverseStream(r, sc, sc, r);
    apm->set_stream_delay_ms(0);
    apm->ProcessStream(m, sc, sc, m);
    std::memcpy(&full[i * 160], m, sizeof m);
    // Linear output is float in [-1, 1], not int16 scale.
    if (apm->GetLinearAecOutput(rtc::ArrayView<std::array<float, 160>>(&lbuf, 1))) {
      for (int k = 0; k < 160; k++)
        lin[i * 160 + k] = (int16_t)std::lround(std::fmax(-32768.f, std::fmin(32767.f, lbuf[k] * 32768.f)));
    }
  }
  writeWav(d + "/aec3_full.wav", full);
  writeWav(d + "/aec3_linear.wav", lin);
  std::cerr << d << ": " << n / 100.0 << "s\n";
}
