<script lang="ts">
  export let className = 'size-5';
  export let strokeWidth = '1.5';
</script>

<svg
  xmlns="http://www.w3.org/2000/svg"
  fill="none"
  viewBox="0 0 24 24"
  aria-hidden="true"
  stroke-width={strokeWidth}
  stroke="currentColor"
  class={className}
>
  <!-- 书本左页 -->
  <path
    stroke-linecap="round"
    stroke-linejoin="round"
    d="M3 5a1 1 0 0 1 1-1h7v15H4a1 1 0 0 1-1-1V5Z"
  />
  <!-- 书本右页 -->
  <path
    stroke-linecap="round"
    stroke-linejoin="round"
    d="M21 5a1 1 0 0 0-1-1h-7v15h7a1 1 0 0 0 1-1V5Z"
  />
  <!-- 书脊 -->
  <line x1="12" y1="4" x2="12" y2="19" stroke-linecap="round" />
  <!-- A (源语言) -->
  <text
    x="7" y="14.5"
    text-anchor="middle"
    font-size="5.5"
    font-weight="600"
    stroke="none"
    fill="currentColor"
    font-family="sans-serif"
  >A</text>
  <!-- 文 (目标语言) -->
  <text
    x="17" y="14.5"
    text-anchor="middle"
    font-size="5.5"
    font-weight="600"
    stroke="none"
    fill="currentColor"
    font-family="sans-serif"
  >文</text>
  <!-- 底部双向切换箭头 -->
  <path
    stroke-linecap="round"
    stroke-linejoin="round"
    d="M6 21.5h4M14 21.5h4M11 20.5l1-1.5 1 1.5"
  />
</svg>