<template>
  <div class="glass-panel rounded-2xl overflow-hidden shadow-xl">
    <div class="px-6 py-4 border-b border-white/5 flex flex-wrap items-center justify-between gap-3 bg-slate-900/40">
      <div>
        <h2 class="text-base font-bold text-white tracking-tight">Active Browser Tabs & Lease Pool</h2>
        <p class="text-xs text-slate-400">Persistent Playwright tabs mapped to IDE conversations and requests</p>
      </div>

      <div class="flex items-center space-x-3">
        <span class="px-2.5 py-1 rounded-full text-xs font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
          {{ store.queueItems.length }} active tab{{ store.queueItems.length === 1 ? '' : 's' }}
        </span>

        <button
          @click="store.clearIdleQueue()"
          class="px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white text-xs font-semibold border border-white/10 transition-all cursor-pointer flex items-center space-x-1.5 hover:scale-105 active:scale-95"
          title="Evict idle tabs to reclaim memory"
        >
          <Trash2 class="w-3.5 h-3.5 text-rose-400" />
          <span>Clear Idle Tabs</span>
        </button>
      </div>
    </div>

    <!-- Empty State -->
    <div v-if="store.queueItems.length === 0" class="py-16 px-6 text-center">
      <div class="w-12 h-12 rounded-full bg-slate-800/80 flex items-center justify-center mx-auto mb-3 text-slate-400">
        <Inbox class="w-6 h-6" />
      </div>
      <p class="text-sm font-semibold text-slate-300">All browser tabs are currently idle</p>
      <p class="text-xs text-slate-500 mt-1 max-w-sm mx-auto">
        When an IDE client sends a request to the completion endpoint, Playwright browser tabs appear here live.
      </p>
    </div>

    <!-- Active Table -->
    <div v-else class="overflow-x-auto">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-900/60 text-slate-400 uppercase font-semibold text-[10px] tracking-wider border-b border-white/5">
          <tr>
            <th class="px-6 py-3">Session Key</th>
            <th class="px-6 py-3">Tab Status</th>
            <th class="px-6 py-3">Lock State</th>
            <th class="px-6 py-3">Idle Time</th>
            <th class="px-6 py-3">URL</th>
            <th class="px-6 py-3 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-white/5">
          <tr v-for="item in store.queueItems" :key="item.session_key" class="hover:bg-white/[0.02] transition-colors">
            <td class="px-6 py-4 font-mono font-medium text-slate-200 max-w-xs truncate select-all">
              {{ item.session_key }}
            </td>
            <td class="px-6 py-4">
              <span
                class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold"
                :class="item.open ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-800 text-slate-500 border border-white/5'"
              >
                {{ item.open ? 'Open' : 'Closed' }}
              </span>
            </td>
            <td class="px-6 py-4">
              <span
                class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold"
                :class="item.locked ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' : 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'"
              >
                {{ item.locked ? 'In Use (Locked)' : 'Available' }}
              </span>
            </td>
            <td class="px-6 py-4 font-mono text-slate-300">
              {{ formatIdle(item.idle_seconds) }}
            </td>
            <td class="px-6 py-4 font-mono text-slate-400 max-w-xs truncate">
              <span v-if="item.url" class="text-cyan-300 hover:underline">{{ item.url }}</span>
              <span v-else class="text-slate-600 italic">None</span>
            </td>
            <td class="px-6 py-4 text-right">
              <button
                @click="copyText(item.session_key)"
                class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition-all cursor-pointer hover:scale-105 active:scale-95"
                title="Copy Session Key"
              >
                <Copy class="w-3.5 h-3.5" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRelayStore } from "../stores/relay"
import { Inbox, Copy, Trash2 } from "lucide-vue-next"

const store = useRelayStore()

function formatIdle(seconds: number) {
  if (typeof seconds !== "number" || isNaN(seconds)) return "-"
  if (seconds < 60) return `${seconds.toFixed(0)}s ago`
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)}m ago`
  return `${(seconds / 3600).toFixed(1)}h ago`
}

function copyText(val: string) {
  navigator.clipboard.writeText(val)
  store.showToast("Copied to clipboard!")
}
</script>
