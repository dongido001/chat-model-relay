<template>
  <div class="fixed bottom-5 right-5 z-50 flex flex-col space-y-2 pointer-events-none">
    <transition-group
      enter-active-class="transform ease-out duration-300 transition"
      enter-from-class="translate-y-2 opacity-0 sm:translate-y-0 sm:translate-x-2"
      enter-to-class="translate-y-0 opacity-100 sm:translate-x-0"
      leave-active-class="transition ease-in duration-100"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-for="toast in store.toasts"
        :key="toast.id"
        class="pointer-events-auto flex items-center space-x-2.5 px-4 py-3 rounded-xl shadow-2xl border text-xs font-semibold backdrop-blur-md"
        :class="{
          'bg-slate-900/95 border-emerald-500/30 text-emerald-300': toast.type === 'success',
          'bg-slate-900/95 border-rose-500/30 text-rose-300': toast.type === 'error',
          'bg-slate-900/95 border-indigo-500/30 text-indigo-300': toast.type === 'info',
        }"
      >
        <CheckCircle2 v-if="toast.type === 'success'" class="w-4 h-4 text-emerald-400" />
        <AlertCircle v-else-if="toast.type === 'error'" class="w-4 h-4 text-rose-400" />
        <Info v-else class="w-4 h-4 text-indigo-400" />
        <span>{{ toast.text }}</span>
      </div>
    </transition-group>
  </div>
</template>

<script setup lang="ts">
import { useRelayStore } from "../stores/relay"
import { CheckCircle2, AlertCircle, Info } from "lucide-vue-next"

const store = useRelayStore()
</script>
