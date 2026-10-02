<template>
  <div class="glass-panel rounded-2xl overflow-hidden shadow-xl flex flex-col h-[600px]">
    <!-- Logs Header & Controls -->
    <div class="px-6 py-3.5 border-b border-white/5 bg-slate-900/60 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center space-x-2">
        <Terminal class="w-4 h-4 text-indigo-400" />
        <h2 class="text-sm font-bold text-white tracking-tight">Real-Time Server Logs</h2>
      </div>

      <!-- Filter toolbar -->
      <div class="flex items-center space-x-2">
        <!-- Level Dropdown -->
        <select
          v-model="store.logLevel"
          class="bg-slate-950 border border-white/10 rounded-lg px-2.5 py-1 text-xs text-slate-300 font-mono focus:border-indigo-500 outline-none cursor-pointer hover:border-white/20 transition-colors"
        >
          <option value="ALL">ALL LEVELS</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>

        <!-- Search Input -->
        <input
          v-model="store.logFilter"
          type="text"
          placeholder="Filter logs..."
          class="bg-slate-950 border border-white/10 rounded-lg px-3 py-1 text-xs text-slate-300 font-mono placeholder:text-slate-600 focus:border-indigo-500 outline-none w-36 sm:w-48"
        />

        <!-- Auto-scroll toggle -->
        <button
          @click="store.autoScroll = !store.autoScroll"
          :class="[
            store.autoScroll ? 'bg-indigo-600/20 text-indigo-400 border-indigo-500/30' : 'bg-slate-900 text-slate-500 border-white/5',
            'px-2.5 py-1 rounded-lg border text-xs font-medium transition-all cursor-pointer hover:scale-105 active:scale-95'
          ]"
        >
          Auto-scroll: {{ store.autoScroll ? "ON" : "OFF" }}
        </button>
      </div>
    </div>

    <!-- Logs Console -->
    <div
      ref="logContainer"
      class="flex-1 p-4 bg-[#070A10] font-mono text-xs overflow-y-auto space-y-1 select-text"
    >
      <div v-if="store.filteredLogs.length === 0" class="text-slate-600 italic py-8 text-center">
        No log entries match the current filter.
      </div>
      <div
        v-for="(log, idx) in store.filteredLogs"
        :key="idx"
        class="leading-relaxed hover:bg-white/[0.02] px-2 py-0.5 rounded transition-colors whitespace-pre-wrap break-all"
        :class="getLogClass(log)"
      >
        {{ log }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from "vue"
import { useRelayStore } from "../stores/relay"
import { Terminal } from "lucide-vue-next"

const store = useRelayStore()
const logContainer = ref<HTMLElement | null>(null)

watch(
  () => store.logs.length,
  async () => {
    if (store.autoScroll && logContainer.value) {
      await nextTick()
      logContainer.value.scrollTop = logContainer.value.scrollHeight
    }
  }
)

function getLogClass(line: string) {
  if (line.includes("| ERROR")) return "text-rose-400 bg-rose-500/5"
  if (line.includes("| WARNING")) return "text-amber-400 bg-amber-500/5"
  if (line.includes("POST /v1/chat/completions")) return "text-cyan-300 font-semibold"
  if (line.includes("Derived conversation id")) return "text-indigo-300"
  if (line.includes("Response received")) return "text-emerald-400"
  return "text-slate-400"
}
</script>
