<template>
  <div class="glass-panel rounded-2xl p-6 relative overflow-hidden border border-white/5">
    <div class="absolute -right-16 -top-16 w-64 h-64 bg-indigo-500/10 rounded-full blur-3xl pointer-events-none" />

    <div class="space-y-4">
      <!-- Title & Main Copy Field -->
      <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <div class="flex items-center space-x-2">
            <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse mr-1.5" />
              Relay Online
            </span>
            <span class="text-xs text-slate-400 font-mono">Port {{ baseUrl.split(':').pop() }}</span>
          </div>
          <h2 class="text-lg font-bold text-white tracking-tight mt-1">OpenAI-Compatible Chat Endpoint</h2>
          <p class="text-xs text-slate-400">Point Cursor, Copilot, Cline, Continue.dev, or any OpenAI client to this base URL</p>
        </div>

        <button
          @click="copyEndpoint"
          class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 active:scale-95 text-white font-medium text-xs shadow-lg shadow-indigo-600/20 transition-all flex items-center space-x-1.5 cursor-pointer"
        >
          <component :is="copied ? Check : Copy" class="w-3.5 h-3.5" />
          <span>{{ copied ? "Copied!" : "Copy Endpoint URL" }}</span>
        </button>
      </div>

      <!-- Quick Link Bar -->
      <div class="flex items-center justify-between pt-2 border-t border-white/5">
        <span class="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Quick Connect & Tools</span>
        <button
          @click="showVncInfoModal = true"
          class="text-[11px] text-indigo-400 hover:text-indigo-300 underline cursor-pointer"
        >
          VNC Info & Browser Status
        </button>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 pt-1">
        <!-- ChatGPT VNC / Browser GUI -->
        <div
          @click="handleVncClick('chatgpt')"
          class="group p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800/90 border border-white/5 hover:border-emerald-500/40 transition-all flex items-center justify-between cursor-pointer"
          :title="isChatGptVncOpen ? 'Open ChatGPT noVNC web viewer' : 'Relay running natively: Click for browser guidance'"
        >
          <div class="flex items-center space-x-2.5">
            <div class="w-7 h-7 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
              <Monitor class="w-3.5 h-3.5" />
            </div>
            <div>
              <div class="text-xs font-bold text-slate-200 group-hover:text-white flex items-center space-x-1.5">
                <span>ChatGPT Browser</span>
                <span
                  class="w-1.5 h-1.5 rounded-full"
                  :class="isChatGptVncOpen ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'"
                />
              </div>
              <div class="text-[10px] font-mono" :class="isChatGptVncOpen ? 'text-emerald-400' : 'text-slate-400'">
                {{ isChatGptVncOpen ? ':5800 • noVNC Active' : 'Native Mac Desktop' }}
              </div>
            </div>
          </div>
          <ExternalLink class="w-3.5 h-3.5 text-slate-500 group-hover:text-emerald-300 transition-colors" />
        </div>

        <!-- Gemini VNC / Browser GUI -->
        <div
          @click="handleVncClick('gemini')"
          class="group p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800/90 border border-white/5 hover:border-indigo-500/40 transition-all flex items-center justify-between cursor-pointer"
          :title="isGeminiVncOpen ? 'Open Gemini noVNC web viewer' : 'Relay running natively: Click for browser guidance'"
        >
          <div class="flex items-center space-x-2.5">
            <div class="w-7 h-7 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
              <Monitor class="w-3.5 h-3.5" />
            </div>
            <div>
              <div class="text-xs font-bold text-slate-200 group-hover:text-white flex items-center space-x-1.5">
                <span>Gemini Browser</span>
                <span
                  class="w-1.5 h-1.5 rounded-full"
                  :class="isGeminiVncOpen ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'"
                />
              </div>
              <div class="text-[10px] font-mono" :class="isGeminiVncOpen ? 'text-emerald-400' : 'text-slate-400'">
                {{ isGeminiVncOpen ? ':5801 • noVNC Active' : 'Native Mac Desktop' }}
              </div>
            </div>
          </div>
          <ExternalLink class="w-3.5 h-3.5 text-slate-500 group-hover:text-indigo-300 transition-colors" />
        </div>

        <!-- Swagger /docs -->
        <a
          :href="docsUrl"
          target="_blank"
          class="group p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800/90 border border-white/5 hover:border-indigo-500/40 transition-all flex items-center justify-between cursor-pointer"
        >
          <div class="flex items-center space-x-2.5">
            <div class="w-7 h-7 rounded-lg bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-400">
              <FileCode class="w-3.5 h-3.5" />
            </div>
            <div>
              <div class="text-xs font-bold text-slate-200 group-hover:text-white">API Swagger Docs</div>
              <div class="text-[10px] font-mono text-slate-400">/docs &bull; Interactive UI</div>
            </div>
          </div>
          <ExternalLink class="w-3.5 h-3.5 text-slate-500 group-hover:text-indigo-300 transition-colors" />
        </a>

        <!-- Ollama Endpoint -->
        <div
          @click="copyText(ollamaUrl)"
          class="group p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800/90 border border-white/5 hover:border-indigo-500/40 transition-all flex items-center justify-between cursor-pointer"
          title="Click to copy Ollama native endpoint URL"
        >
          <div class="flex items-center space-x-2.5">
            <div class="w-7 h-7 rounded-lg bg-cyan-500/10 border border-cyan-500/20 flex items-center justify-center text-cyan-400">
              <Terminal class="w-3.5 h-3.5" />
            </div>
            <div>
              <div class="text-xs font-bold text-slate-200 group-hover:text-white">Ollama API</div>
              <div class="text-[10px] font-mono text-cyan-300">/api/chat &bull; Native</div>
            </div>
          </div>
          <Copy class="w-3.5 h-3.5 text-slate-500 group-hover:text-cyan-300 transition-colors" />
        </div>
      </div>
    </div>

    <!-- Supported Model Badges -->
    <div class="mt-4 pt-4 border-t border-white/5 flex flex-wrap items-center gap-2">
      <span class="text-xs text-slate-400 font-medium mr-1">Available Models:</span>
      <span
        v-for="model in models"
        :key="model.name"
        class="inline-flex items-center space-x-1 px-2.5 py-1 rounded-lg text-xs font-mono bg-slate-900/90 border border-white/10 text-slate-300 hover:border-indigo-500/50 hover:bg-slate-800/80 transition-all cursor-pointer select-none active:scale-95"
      >
        <span class="w-1.5 h-1.5 rounded-full" :class="model.active ? 'bg-indigo-400' : 'bg-slate-500'" />
        <span>{{ model.name }}</span>
      </span>
    </div>

    <!-- VNC / Browser Guidance Modal -->
    <div
      v-if="showVncInfoModal"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
    >
      <div class="glass-panel-elevated max-w-xl w-full rounded-2xl p-6 space-y-4 shadow-2xl border border-white/10 max-h-[90vh] overflow-y-auto">
        <div class="flex items-center justify-between border-b border-white/5 pb-3">
          <div class="flex items-center space-x-2.5 text-cyan-400">
            <MonitorPlay class="w-5 h-5" />
            <h3 class="text-base font-bold text-white tracking-tight">Browser Access & VNC Status</h3>
          </div>
          <button
            @click="showVncInfoModal = false"
            class="p-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white cursor-pointer"
          >
            <X class="w-4 h-4" />
          </button>
        </div>

        <div class="space-y-4 text-xs text-slate-300 leading-relaxed">
          <!-- Runtime Mode Banner -->
          <div
            class="p-3.5 rounded-xl border flex items-start space-x-3"
            :class="isAnyVncOpen ? 'bg-emerald-950/40 border-emerald-500/30 text-emerald-200' : 'bg-amber-950/40 border-amber-500/30 text-amber-200'"
          >
            <div class="w-2 h-2 rounded-full mt-1 flex-shrink-0" :class="isAnyVncOpen ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'" />
            <div>
              <div class="font-bold text-white text-sm">
                {{ isAnyVncOpen ? 'Docker noVNC Web Server Active' : 'Native macOS Mode (No VNC Needed)' }}
              </div>
              <div class="text-xs text-slate-300 mt-1 leading-normal">
                <template v-if="!isAnyVncOpen">
                  The relay is currently running directly on your Mac. Google Chrome opens <strong>directly on your macOS desktop</strong>. Look for Chrome in your Dock or switch to it with <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-white/10 font-mono text-[11px]">Cmd + Tab</kbd>.
                </template>
                <template v-else>
                  The containerized TigerVNC + noVNC server is running. You can view and control browser sessions through your web browser on ports 5800 and 5801.
                </template>
              </div>
            </div>
          </div>

          <!-- Why VNC is refused explanation -->
          <div v-if="!isAnyVncOpen" class="space-y-2">
            <div class="font-semibold text-slate-200 flex items-center space-x-1.5">
              <span>Why did http://127.0.0.1:5800 or :5801 say "Connection Refused"?</span>
            </div>
            <p class="text-slate-400">
              Ports <strong>5800</strong> and <strong>5801</strong> only exist when running Chat Model Relay via <strong>Docker Compose</strong> (<code class="text-cyan-300 bg-slate-900 px-1 py-0.5 rounded">docker compose up -d</code>). Outside of Docker, Chrome is a regular macOS window on your screen, so no virtual web VNC server is started.
            </p>
          </div>

          <!-- Ports overview -->
          <div class="bg-slate-950 p-3.5 rounded-xl border border-white/5 space-y-2.5 font-mono text-[11px]">
            <div class="flex items-center justify-between">
              <span class="text-slate-400">ChatGPT Browser GUI:</span>
              <div class="flex items-center space-x-2">
                <span class="text-[10px] px-1.5 py-0.5 rounded border" :class="isChatGptVncOpen ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-slate-800 text-slate-400 border-white/5'">
                  {{ isChatGptVncOpen ? 'Listening' : 'Host Desktop Window' }}
                </span>
                <a href="http://127.0.0.1:5800" target="_blank" class="text-cyan-300 hover:underline">:5800</a>
              </div>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-slate-400">Gemini Browser GUI:</span>
              <div class="flex items-center space-x-2">
                <span class="text-[10px] px-1.5 py-0.5 rounded border" :class="isGeminiVncOpen ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-slate-800 text-slate-400 border-white/5'">
                  {{ isGeminiVncOpen ? 'Listening' : 'Host Desktop Window' }}
                </span>
                <a href="http://127.0.0.1:5801" target="_blank" class="text-cyan-300 hover:underline">:5801</a>
              </div>
            </div>
            <div class="flex items-center justify-between pt-1 border-t border-white/5">
              <span class="text-slate-400">Direct VNC Client Ports:</span>
              <span class="text-slate-300">5900 (ChatGPT) &bull; 5901 (Gemini)</span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-slate-400">VNC Password:</span>
              <span class="text-amber-300">{{ vncPassword || 'None (or set via RELAY_VNC_PASSWORD)' }}</span>
            </div>
          </div>

          <!-- How to enable Docker VNC -->
          <div class="p-3 rounded-xl bg-slate-900/60 border border-white/5 space-y-1.5">
            <div class="font-semibold text-slate-200">To run with noVNC Web Viewers (:5800 / :5801):</div>
            <p class="text-slate-400">
              1. Start Docker Desktop on macOS.<br />
              2. Run in terminal: <code class="text-indigo-300 bg-slate-950 px-1.5 py-0.5 rounded font-mono text-[11px]">docker compose up -d</code><br />
              3. The web viewers will automatically become reachable at <a href="http://127.0.0.1:5800" target="_blank" class="text-cyan-300 underline">:5800</a> and <a href="http://127.0.0.1:5801" target="_blank" class="text-cyan-300 underline">:5801</a>.
            </p>
          </div>
        </div>

        <div class="flex items-center justify-between pt-2 border-t border-white/5">
          <div class="flex items-center space-x-2">
            <a
              href="http://127.0.0.1:5800"
              target="_blank"
              class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-mono transition-colors"
            >
              Open :5800
            </a>
            <a
              href="http://127.0.0.1:5801"
              target="_blank"
              class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-mono transition-colors"
            >
              Open :5801
            </a>
          </div>
          <button
            @click="showVncInfoModal = false"
            class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold cursor-pointer"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue"
import { useRelayStore } from "../stores/relay"
import { Copy, Check, ExternalLink, Monitor, MonitorPlay, FileCode, Terminal, X } from "lucide-vue-next"

const store = useRelayStore()
const copied = ref(false)
const showVncInfoModal = ref(false)

const baseUrl = computed(() => {
  if (typeof window !== "undefined") {
    return window.location.origin.includes(":5173") ? "http://127.0.0.1:8650" : window.location.origin
  }
  return "http://127.0.0.1:8650"
})

const endpointUrl = computed(() => `${baseUrl.value}/v1/chat/completions`)
const docsUrl = computed(() => `${baseUrl.value}/docs`)
const ollamaUrl = computed(() => `${baseUrl.value}/api/chat`)

const vncPassword = computed(() => {
  const item = store.settings.find(s => s.name === "RELAY_VNC_PASSWORD")
  return item?.value || ""
})

const isChatGptVncOpen = computed(() => !!store.overview.vnc?.chatgpt_open)
const isGeminiVncOpen = computed(() => !!store.overview.vnc?.gemini_open)
const isAnyVncOpen = computed(() => !!store.overview.vnc?.available)

function handleVncClick(provider: "chatgpt" | "gemini") {
  const isOpen = provider === "chatgpt" ? isChatGptVncOpen.value : isGeminiVncOpen.value
  if (isOpen) {
    const url = provider === "gemini" ? "http://127.0.0.1:5801" : "http://127.0.0.1:5800"
    window.open(url, "_blank")
  } else {
    showVncInfoModal.value = true
  }
}

const models = [
  { name: "catgpt-browser", active: true },
  { name: "gemini-browser", active: true },
  { name: "gpt-5.6-sol", active: false },
  { name: "gpt-5.5-thinking", active: false },
]

function copyEndpoint() {
  navigator.clipboard.writeText(endpointUrl.value)
  copied.value = true
  store.showToast("Endpoint URL copied to clipboard!")
  setTimeout(() => { copied.value = false }, 2000)
}

function copyText(val: string) {
  navigator.clipboard.writeText(val)
  store.showToast("Copied to clipboard!")
}
</script>