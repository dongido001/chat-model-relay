<template>
  <div class="space-y-6">
    <div class="glass-panel rounded-2xl overflow-hidden shadow-xl">
      <!-- Header -->
      <div class="px-6 py-4 border-b border-white/5 flex flex-wrap items-center justify-between gap-3 bg-slate-900/40">
        <div>
          <h2 class="text-base font-bold text-white tracking-tight">Mapped Project & Conversation Routes</h2>
          <p class="text-xs text-slate-400">Persistent SQLite routes mapping IDE clients and projects to browser chat threads</p>
        </div>

        <div class="flex items-center space-x-3">
          <span class="px-2.5 py-1 rounded-full text-xs font-semibold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
            {{ store.sessions.length }} route{{ store.sessions.length === 1 ? '' : 's' }} stored
          </span>
        </div>
      </div>

      <!-- Search bar -->
      <div class="p-4 border-b border-white/5 bg-slate-950/40 flex items-center justify-between gap-4">
        <div class="relative flex-1 max-w-md">
          <Search class="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            v-model="searchQuery"
            type="text"
            placeholder="Filter by project, app, thread ID, or conversation key..."
            class="w-full bg-slate-900 border border-white/10 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:border-indigo-500 outline-none transition-colors"
          />
        </div>
      </div>

      <!-- Empty State -->
      <div v-if="filteredSessions.length === 0" class="py-16 px-6 text-center">
        <div class="w-12 h-12 rounded-full bg-slate-800/80 flex items-center justify-center mx-auto mb-3 text-slate-400">
          <Monitor class="w-6 h-6" />
        </div>
        <p class="text-sm font-semibold text-slate-300">No conversation routes found</p>
        <p class="text-xs text-slate-500 mt-1 max-w-sm mx-auto">
          When requests execute in VS Code Copilot or Cursor, persistent routes are automatically mapped and stored in SQLite.
        </p>
      </div>

      <!-- Sessions Table -->
      <div v-else class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-900/60 text-slate-400 uppercase font-semibold text-[10px] tracking-wider border-b border-white/5">
            <tr>
              <th class="px-6 py-3">Project / App</th>
              <th class="px-6 py-3">Conversation Key</th>
              <th class="px-6 py-3">Thread ID</th>
              <th class="px-6 py-3">Messages</th>
              <th class="px-6 py-3">Last Updated</th>
              <th class="px-6 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-white/5">
            <tr v-for="s in filteredSessions" :key="s.conversation_key" class="hover:bg-white/[0.02] transition-colors">
              <td class="px-6 py-4">
                <div class="font-medium text-slate-200">{{ s.project_key }}</div>
                <div class="text-[10px] text-slate-500 font-mono">{{ s.app_key }}</div>
              </td>
              <td class="px-6 py-4 font-mono font-medium text-indigo-300 max-w-xs truncate select-all">
                {{ s.conversation_key }}
              </td>
              <td class="px-6 py-4 font-mono text-slate-400 max-w-xs truncate">
                <span v-if="s.thread_id" class="text-cyan-300 bg-cyan-950/40 px-2 py-0.5 rounded border border-cyan-500/20">
                  {{ s.thread_id }}
                </span>
                <span v-else class="text-slate-600 italic">None</span>
              </td>
              <td class="px-6 py-4 font-mono text-slate-300">
                <span class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-white/5 text-[11px]">
                  {{ getMessageCount(s.transcript_json) }} msgs
                </span>
              </td>
              <td class="px-6 py-4 text-slate-400 whitespace-nowrap">
                {{ formatTimestamp(s.updated_at) }}
              </td>
              <td class="px-6 py-4 text-right whitespace-nowrap">
                <div class="inline-flex items-center justify-end space-x-2">
                  <button
                    v-if="s.transcript_json"
                    @click="inspectTranscript(s)"
                    class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-indigo-600 hover:text-white text-slate-300 text-[11px] font-semibold transition-all cursor-pointer inline-flex items-center space-x-1 hover:scale-105 active:scale-95 shrink-0"
                    title="View Message Transcript"
                  >
                    <Eye class="w-3 h-3" />
                    <span>Inspect</span>
                  </button>
                  <button
                    @click="confirmDelete(s)"
                    class="p-1.5 rounded-lg bg-slate-800 hover:bg-rose-950 hover:text-rose-400 text-slate-400 transition-all cursor-pointer inline-flex items-center hover:scale-105 active:scale-95 shrink-0"
                    title="Delete Conversation Route"
                  >
                    <Trash2 class="w-3.5 h-3.5" />
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Transcript Inspection Modal -->
    <div
      v-if="selectedTranscriptSession"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
    >
      <div class="glass-panel-elevated max-w-3xl w-full rounded-2xl flex flex-col max-h-[85vh] shadow-2xl border border-white/10 overflow-hidden">
        <!-- Modal Header -->
        <div class="px-6 py-4 border-b border-white/5 flex items-center justify-between bg-slate-900/60">
          <div>
            <h3 class="text-sm font-bold text-white tracking-tight">Conversation Transcript</h3>
            <p class="text-xs text-slate-400 font-mono">{{ selectedTranscriptSession.conversation_key }}</p>
          </div>
          <button
            @click="selectedTranscriptSession = null"
            class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white transition-all cursor-pointer"
          >
            <X class="w-4 h-4" />
          </button>
        </div>

        <!-- Messages Bubble View -->
        <div class="flex-1 p-6 overflow-y-auto space-y-4 bg-[#070A10]/90">
          <div
            v-for="(msg, idx) in parsedTranscript"
            :key="idx"
            :class="[
              msg.role === 'user' ? 'ml-8 items-end' : 'mr-8 items-start',
              'flex flex-col space-y-1'
            ]"
          >
            <span class="text-[10px] font-bold uppercase tracking-wider text-slate-500 px-1">
              {{ msg.role }}
            </span>
            <div
              :class="[
                msg.role === 'user'
                  ? 'bg-indigo-600/30 border-indigo-500/40 text-slate-100 rounded-2xl rounded-tr-sm'
                  : 'bg-slate-900 border-white/10 text-slate-200 rounded-2xl rounded-tl-sm',
                'px-4 py-3 text-xs border leading-relaxed whitespace-pre-wrap break-words max-w-full'
              ]"
            >
              {{ msg.content }}
            </div>
          </div>
        </div>

        <!-- Modal Footer -->
        <div class="px-6 py-3 border-t border-white/5 bg-slate-900/40 flex justify-end">
          <button
            @click="selectedTranscriptSession = null"
            class="px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold cursor-pointer"
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
import { useRelayStore, type SessionItem } from "../stores/relay"
import { Monitor, Search, Trash2, Eye, X } from "lucide-vue-next"

const store = useRelayStore()
const searchQuery = ref("")
const selectedTranscriptSession = ref<SessionItem | null>(null)

function getMessageCount(transcriptJson?: string) {
  if (!transcriptJson) return 0
  try {
    const parsed = JSON.parse(transcriptJson)
    return Array.isArray(parsed) ? parsed.length : 0
  } catch {
    return 0
  }
}

function formatTimestamp(epoch?: number) {
  if (!epoch) return "-"
  const d = new Date(epoch * 1000)
  return d.toLocaleString()
}

function inspectTranscript(s: SessionItem) {
  selectedTranscriptSession.value = s
}

const parsedTranscript = computed(() => {
  if (!selectedTranscriptSession.value?.transcript_json) return []
  try {
    const list = JSON.parse(selectedTranscriptSession.value.transcript_json)
    return Array.isArray(list) ? list : []
  } catch {
    return []
  }
})

async function confirmDelete(s: SessionItem) {
  if (confirm(`Delete conversation route "${s.conversation_key}"?`)) {
    await store.deleteSession(s.project_key, s.app_key, s.conversation_key)
  }
}

const filteredSessions = computed(() => {
  return store.sessions.filter(s => {
    if (!searchQuery.value.trim()) return true
    const q = searchQuery.value.toLowerCase()
    return (
      s.project_key.toLowerCase().includes(q) ||
      s.app_key.toLowerCase().includes(q) ||
      s.conversation_key.toLowerCase().includes(q) ||
      (s.thread_id || "").toLowerCase().includes(q)
    )
  })
})
</script>
