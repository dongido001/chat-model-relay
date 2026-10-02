<template>
  <div class="min-h-screen flex flex-col">
    <!-- Top Navigation -->
    <HeaderNav />

    <!-- Main Content Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <!-- Endpoint Banner with One-Click Copy -->
      <EndpointBanner />

      <!-- High-level Metric Cards -->
      <MetricsGrid />

      <!-- Active View Content -->
      <transition mode="out-in" enter-active-class="transition duration-150 ease-out" enter-from-class="opacity-0 translate-y-1" enter-to-class="opacity-100 translate-y-0" leave-active-class="transition duration-100 ease-in" leave-from-class="opacity-100" leave-to-class="opacity-0">
        <QueuePanel v-if="store.activeTab === 'queue'" />
        <SessionsPanel v-else-if="store.activeTab === 'sessions'" />
        <LogsPanel v-else-if="store.activeTab === 'logs'" />
        <CompanionsPanel v-else-if="store.activeTab === 'companions'" />
        <SettingsPanel v-else-if="store.activeTab === 'settings'" />
      </transition>
    </main>

    <!-- Footer -->
    <footer class="border-t border-white/5 py-4 text-center text-xs text-slate-500">
      Chat Model Relay Gateway &bull; Auto-routes IDE tool requests to persistent browser sessions
    </footer>

    <!-- Global Toast Notifications -->
    <ToastContainer />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from "vue"
import { useRelayStore } from "./stores/relay"
import HeaderNav from "./components/HeaderNav.vue"
import EndpointBanner from "./components/EndpointBanner.vue"
import MetricsGrid from "./components/MetricsGrid.vue"
import QueuePanel from "./components/QueuePanel.vue"
import SessionsPanel from "./components/SessionsPanel.vue"
import LogsPanel from "./components/LogsPanel.vue"
import CompanionsPanel from "./components/CompanionsPanel.vue"
import SettingsPanel from "./components/SettingsPanel.vue"
import ToastContainer from "./components/ToastContainer.vue"

const store = useRelayStore()
let timer: any = null

const validTabs = new Set(["queue", "sessions", "logs", "companions", "settings"])

function syncTabFromUrl() {
  const section = window.location.pathname.match(/^\/dashboard\/([^/]+)\/?$/)?.[1]
  if (section && validTabs.has(section)) {
    store.activeTab = section as typeof store.activeTab
  } else if (window.location.pathname === "/dashboard" || window.location.pathname === "/dashboard/") {
    store.activeTab = "queue"
  }
}

function handlePopState() {
  syncTabFromUrl()
}

onMounted(async () => {
  syncTabFromUrl()
  window.addEventListener("popstate", handlePopState)
  await store.refreshAll()
  timer = setInterval(() => {
    store.refreshAll()
  }, 2500)
})

onUnmounted(() => {
  window.removeEventListener("popstate", handlePopState)
  if (timer) clearInterval(timer)
})
</script>
