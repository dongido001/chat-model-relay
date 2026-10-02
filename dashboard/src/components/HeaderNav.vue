<template>
  <header class="border-b border-white/5 bg-[#0D121F]/80 backdrop-blur-md sticky top-0 z-40">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <!-- Left: Logo & Title -->
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-cyan-400 p-[1px] shadow-lg shadow-indigo-500/20">
          <div class="w-full h-full bg-[#0B0F19] rounded-[11px] flex items-center justify-center">
            <Radio class="w-5 h-5 text-indigo-400" />
          </div>
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <span class="font-bold text-base text-white tracking-tight">Chat Model Relay</span>
            <span class="text-[10px] font-semibold tracking-wider uppercase px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              Gateway
            </span>
          </div>
          <p class="text-xs text-slate-400 hidden sm:block">OpenAI IDE Protocol &harr; Browser Engine</p>
        </div>
      </div>

      <!-- Center: Navigation Tabs -->
      <nav class="hidden md:flex items-center space-x-1 bg-slate-900/80 p-1 rounded-xl border border-white/5 shadow-inner">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="navigateToTab(tab.id)"
          :class="[
            store.activeTab === tab.id
              ? 'bg-indigo-600 text-white shadow-sm shadow-indigo-500/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-white/5',
            'px-3.5 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 flex items-center space-x-1.5 cursor-pointer hover:scale-[1.02] active:scale-[0.98]'
          ]"
        >
          <component :is="tab.icon" class="w-3.5 h-3.5" />
          <span>{{ tab.label }}</span>
          <span
            v-if="tab.badge !== undefined && tab.badge > 0"
            class="px-1.5 py-0.2 rounded-full text-[10px] font-bold bg-white/20 text-white"
          >
            {{ tab.badge }}
          </span>
        </button>
      </nav>

      <!-- Right: Status & Refresh -->
      <div class="flex items-center space-x-3">
        <div class="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-slate-900/60 border border-white/5">
          <span
            class="w-2 h-2 rounded-full"
            :class="store.isConnected ? 'bg-emerald-400 shadow-sm shadow-emerald-400/50 animate-pulse' : 'bg-rose-500'"
          />
          <span class="text-xs font-medium text-slate-300">
            {{ store.isConnected ? "Live Relay" : "Connecting..." }}
          </span>
        </div>

        <button
          @click="store.refreshAll()"
          :disabled="store.isRefreshing"
          class="p-2 rounded-lg bg-slate-900/60 hover:bg-slate-800 text-slate-400 hover:text-slate-100 border border-white/5 transition-all duration-150 disabled:opacity-50 cursor-pointer hover:scale-105 active:scale-95"
          title="Refresh All Data"
        >
          <RotateCw class="w-4 h-4" :class="{ 'animate-spin': store.isRefreshing }" />
        </button>
      </div>
    </div>

    <!-- Mobile Tabs -->
    <div class="md:hidden flex items-center justify-around border-t border-white/5 px-2 py-2 bg-[#0B0F19] overflow-x-auto">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        @click="navigateToTab(tab.id)"
        :class="[
          store.activeTab === tab.id ? 'text-indigo-400 font-semibold' : 'text-slate-400',
          'text-xs py-1 px-2.5 flex items-center space-x-1 cursor-pointer whitespace-nowrap'
        ]"
      >
        <component :is="tab.icon" class="w-3.5 h-3.5" />
        <span>{{ tab.label }}</span>
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue"
import { useRelayStore } from "../stores/relay"
import { Radio, RotateCw, Layers, Monitor, Terminal, Cpu, Settings } from "lucide-vue-next"

const store = useRelayStore()

type DashboardTab = "queue" | "sessions" | "logs" | "companions" | "settings"

function navigateToTab(tab: DashboardTab) {
  store.activeTab = tab
  const url = `/dashboard/${tab}`
  if (window.location.pathname !== url) {
    window.history.pushState({ tab }, "", url)
  }
}

const tabs = computed(() => [
  { id: "queue" as const, label: "Queue", icon: Layers, badge: store.overview.queue?.waiting || 0 },
  { id: "sessions" as const, label: "Sessions", icon: Monitor, badge: store.sessions.length || store.overview.conversations?.sessions || 0 },
  { id: "logs" as const, label: "Live Logs", icon: Terminal },
  { id: "companions" as const, label: "Companions", icon: Cpu },
  { id: "settings" as const, label: "Settings", icon: Settings },
])
</script>
