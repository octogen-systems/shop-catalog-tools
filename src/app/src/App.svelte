<script>
    import { onMount } from 'svelte';
    import AppRouter from "$components/AppRouter.svelte";
    import './global.css'; // Import the global styles here

    export let url = "";
    let currentCatalog = window.location.pathname.split('/')[1] || 'anntaylor';
    let currentPath = window.location.pathname;

    $: {
        currentPath = window.location.pathname;
    }

    onMount(() => {
        // Handle root path redirect
        if (window.location.pathname === '/') {
            window.location.href = '/anntaylor';
            return;
        }
    });
</script>

<AppRouter {url} {currentCatalog} {currentPath} />