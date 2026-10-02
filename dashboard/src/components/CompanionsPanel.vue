<template>
  <div class="glass-panel rounded-2xl overflow-hidden shadow-xl">
    <div class="px-6 py-4 border-b border-white/5 flex flex-wrap items-center justify-between gap-3 bg-slate-900/40">
      <div>
        <h2 class="text-base font-bold text-white tracking-tight">Multi-Node Relay & Browser Viewers</h2>
        <p class="text-xs text-slate-400">Coordinated browser automation instances with direct native screen or noVNC interactive access</p>
      </div>
      <span class="px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
        {{ onlineCount }} / {{ providers.length }} Nodes Active
      </span>
    </div>

    <div class="p-6 grid grid-cols-1 md:grid-cols-2 gap-6">
      <div
        v-for="p in providers"
        :key="p.id"
        class="bg-slate-900/70 border border-white/5 rounded-2xl p-6 flex flex-col justify-between hover:border-indigo-500/30 transition-all duration-200"
      >
        <div>
          <!-- Header -->
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center space-x-2.5">
              <div class="w-9 h-9 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 font-bold text-sm uppercase">
                {{ p.id.slice(0, 2) }}
              </div>
              <div>
                <span class="font-bold text-base text-white tracking-tight">{{ p.name }} Gateway</span>
                <span v-if="p.id === store.overview.service?.provider" class="ml-2 px-1.5 py-0.5 rounded text-[9px] font-bold uppercase bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                  Primary
                </span>
              </div>
            </div>

            <div class="flex items-center space-x-1.5">
              <span
                class="w-2 h-2 rounded-full"
                :class="p.status === 'online' ? 'bg-emerald-400 shadow-sm shadow-emerald-400/50 animate-pulse' : 'bg-rose-500'"
              />
              <span
                class="text-xs font-semibold capitalize"
                :class="p.status === 'online' ? 'text-emerald-400' : 'text-rose-400'"
              >
                {{ p.status }}
              </span>
            </div>
          </div>

          <!-- Port & VNC Addresses -->
          <div class="bg-slate-950/70 rounded-xl p-3 border border-white/5 space-y-1.5 font-mono text-xs mb-4">
            <div class="flex items-center justify-between">
              <span class="text-slate-500">API Endpoint:</span>
              <span class="text-cyan-300">http://127.0.0.1:{{ p.port }}</span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-slate-500">Browser Display:</span>
              <div class="flex items-center space-x-1.5">
                <span
                  class="w-1.5 h-1.5 rounded-full"
                  :class="isVncPortOpen(p.id) ? 'bg-emerald-400' : 'bg-amber-400'"
                />
                <span v-if="isVncPortOpen(p.id)" class="text-emerald-400 hover:underline cursor-pointer" @click="handleOpenBrowser(p.id)">
                  {{ getVncUrl(p.id) }}
                </span>
                <span v-else class="text-amber-300 cursor-pointer hover:underline text-[11px]" @click="openExplainer(p.id)">
                  Native Mac Desktop App
                </span>
              </div>
            </div>
            <div class="flex items-center justify-between text-[11px]">
              <span class="text-slate-600">VNC Mode / Port:</span>
              <span class="text-slate-400">
                {{ isVncPortOpen(p.id) ? (p.id === 'gemini' ? 'noVNC :5801' : 'noVNC :5800') : 'Docker Required for noVNC' }}
              </span>
            </div>
          </div>

          <!-- Metrics Row -->
          <div class="grid grid-cols-3 gap-2 p-3 rounded-xl bg-slate-950/50 border border-white/5 mb-4 text-center">
            <div>
              <div class="text-[10px] text-slate-500 font-semibold uppercase">Memory</div>
              <div class="text-xs font-mono font-bold text-slate-200">{{ formatMemory(p.memory_bytes) }}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-semibold uppercase">Concurrency</div>
              <div class="text-xs font-mono font-bold text-indigo-300">{{ p.active }} / {{ p.capacity }}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-semibold uppercase">Routes</div>
              <div class="text-xs font-mono font-bold text-emerald-300">{{ p.sessions }}</div>
            </div>
          </div>

          <!-- Models tags -->
          <div class="space-y-1.5">
            <span class="text-[11px] text-slate-500 font-medium">Auto-Routed Models:</span>
            <div class="flex flex-wrap gap-1.5">
              <span
                v-for="m in p.models"
                :key="m"
                class="px-2 py-0.5 rounded-lg bg-slate-800 border border-white/5 text-slate-300 font-mono text-[11px]"
              >
                {{ m }}
              </span>
            </div>
          </div>
        </div>

        <!-- Action Links -->
        <div class="mt-6 pt-4 border-t border-white/5 flex flex-wrap items-center justify-between gap-2">
          <!-- Direct Browser button -->
          <button
            @click="handleOpenBrowser(p.id)"
            class="px-3.5 py-1.5 rounded-xl border text-xs font-semibold transition-all cursor-pointer inline-flex items-center space-x-1.5 hover:scale-105 active:scale-95 shadow-sm"
            :class="isVncPortOpen(p.id) ? 'bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border-emerald-500/30' : 'bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border-amber-500/30'"
          >
            <Monitor class="w-3.5 h-3.5" />
            <span>{{ isVncPortOpen(p.id) ? 'Open Browser VNC' : 'Native Browser Info' }}</span>
            <ExternalLink v-if="isVncPortOpen(p.id)" class="w-3 h-3" />
          </button>

          <!-- Node Dashboard button -->
          <a
            :href="`http://127.0.0.1:${p.port}/dashboard`"
            target="_blank"
            class="px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-white/10 transition-all cursor-pointer inline-flex items-center space-x-1.5 hover:scale-105 active:scale-95"
          >
            <span>Node UI</span>
            <ExternalLink class="w-3 h-3" />
          </a>
        </div>
      </div>
    </div>

    <!-- Explainer Modal -->
    <div
      v-if="modalProvider"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
    >
      <div class="glass-panel-elevated max-w-lg w-full rounded-2xl p-6 space-y-4 shadow-2xl border border-white/10">
        <div class="flex items-center justify-between border-b border-white/5 pb-3">
          <div class="flex items-center space-x-2.5 text-amber-400">
            <Monitor class="w-5 h-5" />
            <h3 class="text-base font-bold text-white tracking-tight">{{ modalProvider === 'gemini' ? 'Gemini' : 'ChatGPT' }} Browser Access</h3>
          </div>
          <button
            @click="modalProvider = null"
            class="p-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white cursor-pointer"
          >
            <X class="w-4 h-4" />
          </button>
        </div>

        <div class="space-y-3 text-xs text-slate-300 leading-relaxed">
          <div class="p-3 rounded-xl bg-amber-950/40 border border-amber-500/30 text-amber-200 font-medium">
            Relay is running natively on macOS (outside Docker).
          </div>
          <p>
            When running natively, Playwright opens Google Chrome <strong>directly as a Mac window on your desktop</strong>.
          </p>
          <ul class="list-disc pl-4 space-y-1.5 text-slate-400">
            <li>Check your macOS Dock or press <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-white/10 font-mono text-[11px]">Cmd + Tab</kbd> to bring Google Chrome into focus.</li>
            <li>Profile directory: <code class="text-indigo-300 font-mono text-[11px]">{{ modalProvider === 'gemini' ? 'browser_data_gemini' : 'browser_data' }}</code>.</li>
            <li>VNC web viewers on port {{ modalProvider === 'gemini' ? '5801' : '5800' }} are only created when running inside Docker containers (<code class="text-indigo-300 font-mono text-[11px]">docker compose up -d</code>).</li>
          </ul>
        </div>

        <div class="flex items-center justify-between pt-2 border-t border-white/5">
          <a
            :href="getVncUrl(modalProvider)"
            target="_blank"
            class="text-[11px] text-slate-400 hover:text-slate-200 underline font-mono"
          >
            Try opening {{ getVncUrl(modalProvider) }} anyway
          </a>
          <button
            @click="modalProvider = null"
            class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold cursor-pointer"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue"
import { useRelayStore, type ProviderItem } from "../stores/relay"
import { ExternalLink, Monitor, X } from "lucide-vue-next"

const store = useRelayStore()
const modalProvider = ref<string | null>(null)

function getVncUrl(providerId: string) {
  return providerId === "gemini" ? "http://127.0.0.1:5801" : "http://127.0.0.1:5800"
}

function isVncPortOpen(providerId: string) {
  if (providerId === "gemini") {
    return !!store.overview.vnc?.gemini_open
  }
  return !!store.overview.vnc?.chatgpt_open
}

function handleOpenBrowser(providerId: string) {
  if (isVncPortOpen(providerId)) {
    window.open(getVncUrl(providerId), "_blank")
  } else {
    modalProvider.value = providerId
  }
}

function openExplainer(providerId: string) {
  modalProvider.value = providerId
}

const providers = computed<ProviderItem[]>(() => {
  const list = store.overview.providers || []
  if (list.length > 0) return list
  return [
    {
      id: "chatgpt",
      name: "ChatGPT",
      port: 8650,
      status: "online",
      models: ["gpt-4o", "catgpt-browser"],
      memory_bytes: 0,
      active: 0,
      capacity: 3,
      sessions: 0,
    },
    {
      id: "gemini",
      name: "Gemini",
      port: 8651,
      status: "online",
      models: ["gemini-browser"],
      memory_bytes: 0,
      active: 0,
      capacity: 3,
      sessions: 0,
    },
  ]
})

const onlineCount = computed(() => {
  return providers.value.filter(p => p.status === "online").length
})

function formatMemory(bytes: number) {
  if (!bytes || bytes === 0) return "0 MB"
  return `${(bytes / 1024 / 1024).toFixed(0)} MB`
}
</script>