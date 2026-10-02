<template>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
    <!-- Queue Slots -->
    <div class="glass-panel rounded-xl p-4.5 flex flex-col justify-between hover:border-indigo-500/30 hover:-translate-y-0.5 hover:shadow-lg hover:shadow-indigo-500/5 transition-all duration-200 cursor-default">
      <div class="flex items-center justify-between text-slate-400 mb-2">
        <span class="text-xs font-semibold uppercase tracking-wider">Queue Concurrency</span>
        <Activity class="w-4 h-4 text-indigo-400" />
      </div>
      <div class="flex items-baseline space-x-2">
        <span class="text-2xl font-bold text-white tracking-tight">
          {{ store.overview.queue?.active ?? 0 }}
        </span>
        <span class="text-xs text-slate-400 font-mono">
          / {{ store.overview.queue?.capacity ?? 3 }} active
        </span>
      </div>
      <div class="mt-2 text-[11px] text-slate-400 flex items-center space-x-1.5">
        <span
          class="w-1.5 h-1.5 rounded-full"
          :class="(store.overview.queue?.waiting ?? 0) > 0 ? 'bg-amber-400 animate-pulse' : 'bg-emerald-400'"
        />
        <span>{{ store.overview.queue?.waiting ?? 0 }} queued request{{ (store.overview.queue?.waiting ?? 0) === 1 ? '' : 's' }}</span>
      </div>
    </div>

    <!-- Browser Tabs & Routes -->
    <div class="glass-panel rounded-xl p-4.5 flex flex-col justify-between hover:border-indigo-500/30 hover:-translate-y-0.5 hover:shadow-lg hover:shadow-indigo-500/5 transition-all duration-200 cursor-default">
      <div class="flex items-center justify-between text-slate-400 mb-2">
        <span class="text-xs font-semibold uppercase tracking-wider">Browser Sessions</span>
        <Globe class="w-4 h-4 text-cyan-400" />
      </div>
      <div class="flex items-baseline space-x-2">
        <span class="text-2xl font-bold text-white tracking-tight">
          {{ store.queueItems?.length ?? 0 }}
        </span>
        <span class="text-xs text-slate-400 font-mono">
          active tabs
        </span>
      </div>
      <div class="mt-2 text-[11px] text-slate-400">
        {{ store.sessions?.length || store.overview.conversations?.sessions || 0 }} mapped IDE project routes
      </div>
    </div>

    <!-- Requests Handled -->
    <div class="glass-panel rounded-xl p-4.5 flex flex-col justify-between hover:border-indigo-500/30 hover:-translate-y-0.5 hover:shadow-lg hover:shadow-indigo-500/5 transition-all duration-200 cursor-default">
      <div class="flex items-center justify-between text-slate-400 mb-2">
        <span class="text-xs font-semibold uppercase tracking-wider">Requests Handled</span>
        <Zap class="w-4 h-4 text-emerald-400" />
      </div>
      <div class="flex items-baseline space-x-2">
        <span class="text-2xl font-bold text-white tracking-tight">
          {{ totalRequests }}
        </span>
        <span class="text-xs text-slate-400">total calls</span>
      </div>
      <div class="mt-2 text-[11px] text-slate-400 flex items-center space-x-1">
        <span class="text-emerald-400 font-medium">Memory: {{ memoryMb }} MB</span>
      </div>
    </div>

    <!-- Avg Latency -->
    <div class="glass-panel rounded-xl p-4.5 flex flex-col justify-between hover:border-indigo-500/30 hover:-translate-y-0.5 hover:shadow-lg hover:shadow-indigo-500/5 transition-all duration-200 cursor-default">
      <div class="flex items-center justify-between text-slate-400 mb-2">
        <span class="text-xs font-semibold uppercase tracking-wider">Avg Latency</span>
        <Clock class="w-4 h-4 text-amber-400" />
      </div>
      <div class="flex items-baseline space-x-2">
        <span class="text-2xl font-bold text-white tracking-tight font-mono">
          {{ avgLatency }}s
        </span>
      </div>
      <div class="mt-2 text-[11px] text-slate-400">
        Loopback response average
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue"
import { useRelayStore } from "../stores/relay"
import { Activity, Globe, Zap, Clock } from "lucide-vue-next"

const store = useRelayStore()

const totalRequests = computed(() => {
  const cnt = store.overview.metrics?.counters || {}
  return (
    cnt["http.duration_seconds.count"] ||
    cnt["http.requests.GET./other.200"] ||
    cnt["http.requests.POST./v1/chat.200"] ||
    0
  )
})

const memoryMb = computed(() => {
  const bytes = store.overview.service?.memory_bytes || 0
  return (bytes / 1024 / 1024).toFixed(0)
})

const avgLatency = computed(() => {
  const avg = store.overview.metrics?.averages?.["http.duration_seconds.average"]
  if (typeof avg === "number" && !isNaN(avg)) {
    return avg.toFixed(2)
  }
  return "0.00"
})
</script>
