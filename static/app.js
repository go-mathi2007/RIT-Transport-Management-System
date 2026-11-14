
// ======================================================
// FEATURE 1: REAL-TIME MORNING & RETURN BUS FILTERING
// ======================================================

window.cachedMorningBuses = [];
window.cachedReturnBuses = [];
window.defaultReturnTimeStr = "3:50 PM";

function renderMorningBuses(buses) {
    const container = document.getElementById("morningBusListContainer");
    if (!container) return;
    if (!buses || !Array.isArray(buses) || buses.length === 0) {
        container.innerHTML = '<div class="empty-card" style="text-align:center; padding:25px; color:#64748b; font-weight:600;">No buses found matching your search.</div>';
        return;
    }
    container.innerHTML = buses.map(function(b) {
        return `
            <div class="route-card">
                <div class="route-card-header">
                    <div>
                        <h3>🚌 Bus ${escapeHTML(b.bus_number || "---")}</h3>
                        <p class="route-name">${escapeHTML(b.route_name || "---")}</p>
                    </div>
                    <span class="time-badge" style="background:#e0f2fe; color:#0369a1;">MORNING ROUTE</span>
                </div>
                <div class="route-details">
                    <p>🛣️ <strong>Route:</strong> ${escapeHTML(b.route_name || "---")}</p>
                    <p style="margin-top:8px;">📍 <strong>Morning Boarding Points & Timings:</strong></p>
                    <div style="margin-top:8px; display:flex; flex-wrap:wrap; gap:8px;">
                        ${b.stops && b.stops.length > 0 ? b.stops.map(function(s) {
                            return `<span style="background:#f1f5f9; border:1px solid #e2e8f0; padding:5px 10px; border-radius:6px; font-size:12px; color:#1e293b;">
                                <strong>${escapeHTML(s.stop_name)}</strong> <span style="color:#075fce; font-weight:700;">(${escapeHTML(s.stop_time)})</span>
                            </span>`;
                        }).join("") : '<span style="color:#64748b; font-size:12px;">No stops listed</span>'}
                    </div>
                </div>
            </div>
        `;
    }).join("");
}

function filterMorningBuses(query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) {
        renderMorningBuses(window.cachedMorningBuses);
        return;
    }
    const filtered = (window.cachedMorningBuses || []).filter(function(b) {
        const num = (b.bus_number || "").toLowerCase();
        const route = (b.route_name || "").toLowerCase();
        return num.includes(q) || route.includes(q);
    });
    renderMorningBuses(filtered);
}

function renderReturnBuses(buses) {
    const container = document.getElementById("returnBusList");
    if (!container) return;
    container.innerHTML = "";
    if (!buses || !Array.isArray(buses) || buses.length === 0) {
        container.innerHTML = '<div class="empty-card" style="text-align:center; padding:25px; color:#64748b; font-weight:600;">No buses found matching your search.</div>';
        return;
    }
    buses.forEach(function(bus) {
        const card = document.createElement("div");
        card.className = "route-card";
        const departure = bus.departure_time || window.defaultReturnTimeStr || "3:50 PM";
        card.innerHTML = `
            <div class="route-card-header">
                <div>
                    <h3>🚌 Bus ${escapeHTML(bus.bus_number || "---")}</h3>
                    <p class="route-name">${escapeHTML(bus.route_name || "---")}</p>
                </div>
                <span class="time-badge">${escapeHTML(departure)}</span>
            </div>
            <div class="route-details">
                <p>🛣️ <strong>Route:</strong> ${escapeHTML(bus.route_name || "---")}</p>
                <p>🕐 <strong>Departure:</strong> <span class="highlight">${escapeHTML(departure)}</span></p>
            </div>
        `;
        container.appendChild(card);
    });
}

function filterReturnBuses(query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) {
        renderReturnBuses(window.cachedReturnBuses);
        return;
    }
    const filtered = (window.cachedReturnBuses || []).filter(function(b) {
        const num = (b.bus_number || "").toLowerCase();
        const route = (b.route_name || "").toLowerCase();
        return num.includes(q) || route.includes(q);
    });
    renderReturnBuses(filtered);
}

// ======================================================
// BUSMATE STUDENT PORTAL
// ======================================================


// ======================================================
// HTML ESCAPE
// ======================================================

function escapeHTML(value) {

    if (value === null || value === undefined) {
        return "";
    }

    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}


// ======================================================
// PORTAL NOTICES
// ======================================================

async function loadPortalNotices(containerId) {

    const container = document.getElementById(containerId);

    if (!container) return;

    container.innerHTML = '<div class="notice-empty">🔄 Loading notices...</div>';

    try {
        const response = await fetch("/api/notices");
        const data = await response.json();

        if (!data.success || !Array.isArray(data.notices) || data.notices.length === 0) {
            container.innerHTML = '<div class="notice-empty">📢 No new transport notices.</div>';
            return;
        }

        container.innerHTML = data.notices.map(function(notice) {

            const priority = String(notice.priority || "NORMAL").toLowerCase();

            return `
                <article class="notice-card notice-${escapeHTML(priority)}">
                    <div class="notice-card-header">
                        <h4>📢 ${escapeHTML(notice.title)}</h4>
                        <span class="notice-priority ${escapeHTML(priority)}">
                            ${escapeHTML(notice.priority || "NORMAL")}
                        </span>
                    </div>

                    <p>${escapeHTML(notice.message)}</p>

                    <div class="notice-meta">
                        ${escapeHTML(notice.created_at || "")}
                    </div>
                </article>
            `;

        }).join("");

    } catch (error) {
        console.error("Notice load error:", error);
        container.innerHTML = '<div class="notice-empty">Unable to load notices. Please try again.</div>';
    }
}


// ======================================================
// LOAD ALL BUS ROUTES
// IMPORTANT:
// Stops are NOT loaded here.
// Stops load only when user clicks
// "View Boarding Points".
// ======================================================

async function loadRoutesWithStops() {

    const container =
        document.getElementById("routeList");


    if (!container) {
        return;
    }


    container.innerHTML =
        '<p class="loading">🚌 Loading bus routes...</p>';


    try {

        const response =
            await fetch("/api/buses", {
                method: "GET",
                headers: {
                    "Accept": "application/json"
                }
            });


        if (!response.ok) {

            throw new Error(
                "HTTP " + response.status
            );

        }


        const data =
            await response.json();


        container.innerHTML = "";


        if (
            !data.success ||
            !Array.isArray(data.buses) ||
            data.buses.length === 0
        ) {

            container.innerHTML = `
                <div class="empty-card">
                    🚌 No bus routes available.
                </div>
            `;

            return;
        }


        data.buses.forEach(function(bus) {

            const busNumber =
                bus.bus_number || "---";


            const routeName =
                bus.route_name || "Route not available";


            const card =
                document.createElement("div");


            card.className =
                "route-card";


            card.innerHTML = `

                <div class="route-card-header">

                    <div>

                        <h3>
                            🚌 ${escapeHTML(busNumber)}
                        </h3>

                        <p class="route-name">
                            ${escapeHTML(routeName)}
                        </p>

                    </div>

                </div>


                <div class="route-details">

                    <p>
                        🛣️
                        <strong>Route:</strong>
                        ${escapeHTML(routeName)}
                    </p>


                    <button
                        type="button"
                        class="boarding-toggle">

                        📍 View Boarding Points

                    </button>


                    <div class="boarding-panel">

                        <h4 class="boarding-title">

                            📍 Boarding Points -
                            ${escapeHTML(busNumber)}

                        </h4>


                        <div class="boarding-points">

                        </div>

                    </div>

                </div>

            `;


            container.appendChild(card);


            const button =
                card.querySelector(
                    ".boarding-toggle"
                );


            const panel =
                card.querySelector(
                    ".boarding-panel"
                );


            const stopsContainer =
                card.querySelector(
                    ".boarding-points"
                );


            // ------------------------------------------------
            // HEADER & BUTTON CLICK TO VIEW ROUTE & STOPS
            // ------------------------------------------------
            const header =
                card.querySelector(
                    ".route-card-header"
                );

            if (header) {
                header.style.cursor = "pointer";
                header.title = "Click to view route and stops";
                header.addEventListener(
                    "click",
                    function() {
                        toggleBoardingPoints(
                            busNumber,
                            button,
                            panel,
                            stopsContainer
                        );
                    }
                );
            }

            button.addEventListener(
                "click",
                function(e) {
                    e.stopPropagation();
                    toggleBoardingPoints(
                        busNumber,
                        button,
                        panel,
                        stopsContainer
                    );
                }
            );

        });


    } catch (error) {

        console.error(
            "Bus route error:",
            error
        );


        container.innerHTML = `

            <div class="error-box">

                ❌ Unable to load bus routes.

                <br><br>

                Please try again.

            </div>

        `;

    }

}


// ======================================================
// TOGGLE BOARDING POINTS
// ======================================================

async function toggleBoardingPoints(
    busNumber,
    button,
    panel,
    stopsContainer
) {


    // -----------------------------------------------
    // If already visible -> hide
    // -----------------------------------------------

    if (
        panel.classList.contains("show")
    ) {

        panel.classList.remove("show");

        button.textContent =
            "📍 View Boarding Points";

        return;
    }


    // -----------------------------------------------
    // Show panel
    // -----------------------------------------------

    panel.classList.add("show");


    button.textContent =
        "🔼 Hide Boarding Points";


    // -----------------------------------------------
    // If already loaded, don't fetch again
    // -----------------------------------------------

    if (
        panel.dataset.loaded === "true"
    ) {

        return;
    }


    button.classList.add("loading");

    button.textContent =
        "⏳ Loading Boarding Points...";


    stopsContainer.innerHTML = `

        <p class="loading">
            📍 Loading ${escapeHTML(busNumber)} stops...
        </p>

    `;


    try {

        const response =
            await fetch(
                `/api/bus/${encodeURIComponent(busNumber)}/stops`,
                {
                    method: "GET",
                    headers: {
                        "Accept": "application/json"
                    }
                }
            );


        if (!response.ok) {

            throw new Error(
                "HTTP " + response.status
            );

        }


        const data =
            await response.json();


        stopsContainer.innerHTML = "";


        // -------------------------------------------
        // No stops
        // -------------------------------------------

        if (
            !data.success ||
            !Array.isArray(data.stops) ||
            data.stops.length === 0
        ) {

            stopsContainer.innerHTML = `

                <div class="empty-card">

                    📍 No boarding points available
                    for
                    <strong>
                        ${escapeHTML(busNumber)}
                    </strong>.

                </div>

            `;

        }


        // -------------------------------------------
        // Display only THIS bus stops
        // -------------------------------------------

        else {

            data.stops.forEach(
                function(stop, index) {

                    const item =
                        document.createElement("div");


                    item.className =
                        "stop-item";


                    const stopName =
                        stop.stop_name ||
                        "Unknown stop";


                    const stopTime =
                        stop.stop_time ||
                        "";


                    item.innerHTML = `

                        <span class="stop-icon">

                            📍

                        </span>


                        <div class="stop-info">

                            <strong>

                                ${index + 1}.
                                ${escapeHTML(stopName)}

                            </strong>


                            ${
                                stopTime
                                ?
                                `
                                <small>

                                    🕐
                                    ${escapeHTML(stopTime)}

                                </small>
                                `
                                :
                                ""
                            }

                        </div>

                    `;


                    stopsContainer.appendChild(
                        item
                    );

                }
            );

        }


        panel.dataset.loaded = "true";


        button.classList.remove(
            "loading"
        );


        button.textContent =
            "🔼 Hide Boarding Points";


    } catch (error) {

        console.error(
            "Boarding points error:",
            error
        );


        stopsContainer.innerHTML = `

            <div class="error-box">

                ❌ Unable to load boarding points.

                <br>

                Please try again.

            </div>

        `;


        button.classList.remove(
            "loading"
        );


        button.textContent =
            "📍 View Boarding Points";

    }

}


// ======================================================
// SEARCH BUS BY BOARDING POINT
// ======================================================

async function searchBoardingPoint() {
    const input = document.getElementById("boardingPoint");
    const result = document.getElementById("searchResults");

    if (!input || !result) {
        return;
    }

    const stop = input.value.trim();

    if (!stop) {
        result.innerHTML = `
            <div class="warning-box">
                ⚠️ Please enter a boarding point name to search.
            </div>
        `;
        return;
    }

    result.innerHTML = `
        <p class="loading">
            🔍 Searching buses for "${escapeHTML(stop)}"...
        </p>
    `;

    try {
        const response = await fetch(
            `/api/search?stop=${encodeURIComponent(stop)}`,
            {
                method: "GET",
                headers: { "Accept": "application/json" }
            }
        );

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const data = await response.json();
        result.innerHTML = "";

        const items = Array.isArray(data.results)
            ? data.results
            : (Array.isArray(data.buses) ? data.buses : []);

        if (!data.success || items.length === 0) {
            result.innerHTML = `
                <div class="empty-card" style="text-align:center; padding:24px; color:#64748b; font-weight:600;">
                    🚌 No buses found for "<strong>${escapeHTML(stop)}</strong>".
                </div>
            `;
            return;
        }

        items.forEach(function(bus) {
            const busNumber = bus.bus_number || "---";
            const routeName = bus.route_name || "Route details not available";
            const stopName = bus.stop_name || stop;
            const stopTime = bus.stop_time || "---";

            const card = document.createElement("div");
            card.className = "route-card";
            card.innerHTML = `
                <div class="route-card-header" style="cursor:pointer;" title="Click to view full route and stops">
                    <div>
                        <h3 style="cursor:pointer;">
                            🚌 ${escapeHTML(busNumber)}
                        </h3>
                        <p class="route-name">
                            ${escapeHTML(routeName)}
                        </p>
                    </div>
                    <span class="badge badge-active" style="font-size:12px; font-weight:700;">Morning: ${escapeHTML(stopTime)}</span>
                </div>

                <div class="route-details">
                    <p>
                        📍 <strong>Boarding Point:</strong> ${escapeHTML(stopName)}
                    </p>
                    <p>
                        🕐 <strong>Morning Bus Timing:</strong> ${escapeHTML(stopTime)}
                    </p>
                    <button type="button" class="boarding-toggle" style="margin-top:8px;">
                        📍 View Full Route & Stops
                    </button>
                    <div class="boarding-panel">
                        <h4 class="boarding-title">
                            📍 Route Stops - ${escapeHTML(busNumber)}
                        </h4>
                        <div class="boarding-points"></div>
                    </div>
                </div>
            `;

            result.appendChild(card);

            const header = card.querySelector(".route-card-header");
            const button = card.querySelector(".boarding-toggle");
            const panel = card.querySelector(".boarding-panel");
            const stopsContainer = card.querySelector(".boarding-points");

            const clickHandler = function() {
                toggleBoardingPoints(busNumber, button, panel, stopsContainer);
            };

            header.addEventListener("click", clickHandler);
            button.addEventListener("click", function(e) {
                e.stopPropagation();
                clickHandler();
            });
        });

    } catch (error) {
        console.error("Search error:", error);
        result.innerHTML = `
            <div class="error-box" style="text-align:center; padding:20px; color:#dc2626;">
                ❌ Error searching boarding point. Please try again.
            </div>
        `;
    }
}


// ======================================================
// RETURN BUSES
// ======================================================

async function loadReturnBuses() {
    const container = document.getElementById("returnBusList");
    if (!container) return;
    container.innerHTML = '<p class="loading">🔄 Loading return buses...</p>';
    try {
        const response = await fetch("/api/return-buses", {
            method: "GET",
            headers: { "Accept": "application/json" }
        });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();
        if (!data.success || !Array.isArray(data.buses) || data.buses.length === 0) {
            window.cachedReturnBuses = [];
            renderReturnBuses([]);
            return;
        }
        window.cachedReturnBuses = data.buses;
        window.defaultReturnTimeStr = data.default_time || "3:50 PM";
        const searchInput = document.getElementById("returnBusSearchInput");
        if (searchInput && searchInput.value.trim()) {
            filterReturnBuses(searchInput.value);
        } else {
            renderReturnBuses(window.cachedReturnBuses);
        }
    } catch (error) {
        container.innerHTML = '<div class="empty-card" style="color:var(--danger); text-align:center; padding:20px;">⚠️ Unable to load return buses.</div>';
    }
}


// ======================================================
// EXAM TIME BUSES
// ======================================================

async function loadExamBuses() {

    const input =
        document.getElementById(
            "examDate"
        );


    const container =
        document.getElementById(
            "examBusList"
        );


    if (!input || !container) {
        return;
    }


    const htmlDate =
        input.value;


    if (!htmlDate) {

        alert(
            "Please select exam date."
        );

        return;
    }


    const parts =
        htmlDate.split("-");


    const formattedDate =
        `${parts[2]}-${parts[1]}-${parts[0]}`;


    container.innerHTML = `

        <p class="loading">

            📝 Loading exam buses for
            ${escapeHTML(formattedDate)}...

        </p>

    `;


    try {

        const response =
            await fetch(
                `/api/exam-buses?date=${encodeURIComponent(
                    formattedDate
                )}`,
                {
                    method: "GET",
                    headers: {
                        "Accept": "application/json"
                    }
                }
            );


        if (!response.ok) {

            throw new Error(
                "HTTP " + response.status
            );

        }


        const data =
            await response.json();


        container.innerHTML = "";


        if (
            !data.success ||
            !Array.isArray(data.buses) ||
            data.buses.length === 0
        ) {

            container.innerHTML = `

                <div class="empty-card">

                    <h3>
                        📝 No Exam Time Buses
                    </h3>

                    <p>

                        No exam buses available for

                        <strong>
                            ${escapeHTML(
                                formattedDate
                            )}
                        </strong>.

                    </p>

                </div>

            `;

            return;
        }


        data.buses.forEach(
            function(bus) {

                const card =
                    document.createElement("div");


                card.className =
                    "route-card";


                card.innerHTML = `

                    <div class="route-card-header">

                        <div>

                            <h3>

                                📝
                                ${escapeHTML(
                                    bus.bus_number || "---"
                                )}

                            </h3>


                            <p class="route-name">

                                ${escapeHTML(
                                    bus.route_name || "---"
                                )}

                            </p>

                        </div>


                        <span class="time-badge">

                            ${escapeHTML(
                                bus.departure_time || "---"
                            )}

                        </span>

                    </div>


                    <div class="route-details">

                        <p>

                            🛣️
                            <strong>
                                Route:
                            </strong>

                            ${escapeHTML(
                                bus.route_name || "---"
                            )}

                        </p>


                        <p>

                            🕐
                            <strong>
                                Departure:
                            </strong>

                            <span class="highlight">

                                ${escapeHTML(
                                    bus.departure_time || "---"
                                )}

                            </span>

                        </p>


                        <p>

                            📍
                            <strong>
                                Destination:
                            </strong>

                            ${escapeHTML(
                                bus.destination || "---"
                            )}

                        </p>


                        <p>

                            📅
                            <strong>
                                Date:
                            </strong>

                            ${escapeHTML(
                                bus.travel_date ||
                                formattedDate
                            )}

                        </p>

                    </div>

                `;


                container.appendChild(card);

            }
        );


    } catch (error) {

        console.error(
            "Exam bus error:",
            error
        );


        container.innerHTML = `

            <div class="error-box">

                ❌ Error loading exam buses.

            </div>

        `;

    }

}


// ======================================================
// LIVE BUS TRACKING
// ======================================================

async function showLiveBus() {

    const input =
        document.getElementById(
            "liveBusNumber"
        );


    const result =
        document.getElementById(
            "liveBusResult"
        );


    if (!input || !result) {
        return;
    }


    const busNumber =
        input.value
            .trim()
            .toUpperCase();


    if (!busNumber) {

        alert(
            "Please enter bus number."
        );

        return;
    }


    result.innerHTML = `

        <p class="loading">

            📍 Checking live location...

        </p>

    `;


    try {

        const response =
            await fetch(
                `/api/bus-location/${encodeURIComponent(
                    busNumber
                )}`,
                {
                    method: "GET",
                    headers: {
                        "Accept": "application/json"
                    }
                }
            );


        if (!response.ok) {

            throw new Error(
                "HTTP " + response.status
            );

        }


        const data =
            await response.json();


        if (!data.success) {

            result.innerHTML = `

                <div class="empty-card">

                    🚌 Bus

                    <strong>
                        ${escapeHTML(busNumber)}
                    </strong>

                    not found.

                </div>

            `;

            return;
        }


        result.innerHTML = `

            <div class="route-card">

                <div class="route-card-header">

                    <div>

                        <h3>

                            🚌
                            ${escapeHTML(
                                data.bus_number ||
                                busNumber
                            )}

                        </h3>


                        <p class="route-name">

                            ${escapeHTML(
                                data.route_name || "---"
                            )}

                        </p>

                    </div>

                </div>


                <div class="driver-live-details" style="margin:12px 0;padding:12px;border:1px solid #dbe7f3;border-radius:10px;background:#f8fbff;">
                    ${data.location_shared && data.driver_name
                        ? `<strong>👨‍✈️ Driver:</strong> ${escapeHTML(data.driver_name)}${data.driver_phone ? ` &nbsp; <strong>📞</strong> ${escapeHTML(data.driver_phone)}` : ''}`
                        : `<strong>👨‍✈️ Driver:</strong> Not currently sharing location`}
                </div>


                <div class="live-status">

                    ${
                        data.location_shared

                        ?

                        `
                        <span class="status-online">

                            🟢 Live location available

                        </span>
                        `

                        :

                        `
                        <span class="status-offline">

                            🔴 Driver has not shared
                            location yet

                        </span>
                        `
                    }

                </div>


                ${
                    data.location_shared

                    ?

                    `

                    <div class="location-details">

                        <div class="location-item">

                            <span>
                                Latitude
                            </span>

                            <strong>
                                ${escapeHTML(
                                    data.latitude
                                )}
                            </strong>

                        </div>


                        <div class="location-item">

                            <span>
                                Longitude
                            </span>

                            <strong>
                                ${escapeHTML(
                                    data.longitude
                                )}
                            </strong>

                        </div>


                        <div class="location-item">

                            <span>
                                Current Location
                            </span>

                            <strong>
                                ${escapeHTML(
                                    data.current_location ||
                                    "---"
                                )}
                            </strong>

                        </div>


                        <div class="location-item">

                            <span>
                                Next Point
                            </span>

                            <strong>
                                ${escapeHTML(
                                    data.next_point ||
                                    "---"
                                )}
                            </strong>

                        </div>


                        <div class="location-item">

                            <span>
                                Last Updated
                            </span>

                            <strong>
                                ${escapeHTML(
                                    data.updated_at ||
                                    "---"
                                )}
                            </strong>

                        </div>

                    </div>


                    <button
                        type="button"
                        class="map-btn"
                        onclick="openLiveMap('${escapeHTML(
                            data.bus_number ||
                            busNumber
                        )}')">

                        🗺️ Open Live Map

                    </button>

                    `

                    :

                    `

                    <div class="warning-box" style="margin-bottom:14px;">
                        📍 Driver has not started sharing live GPS location yet today.
                    </div>

                    <button
                        type="button"
                        class="map-btn"
                        style="display:inline-flex; align-items:center; gap:8px; background:#082d5c; color:#ffffff; font-weight:700; padding:10px 18px; border-radius:8px; border:none; cursor:pointer;"
                        onclick="openLiveMap('${escapeHTML(
                            data.bus_number ||
                            busNumber
                        )}')">
                        🗺️ Open Live Map Page
                    </button>

                    `

                }

            </div>

        `;


    } catch (error) {

        console.error(
            "Live bus error:",
            error
        );


        result.innerHTML = `

            <div class="error-box">

                ❌ Unable to connect to server.

            </div>

        `;

    }

}


// ======================================================
// OPEN LIVE MAP
// ======================================================

function openLiveMap(busNumber) {
    if (!busNumber) return;
    window.location.href =
        `/live-map?bus=${encodeURIComponent(
            busNumber
        )}`;
}


// ======================================================
// PAGE EVENTS
// ======================================================

document.addEventListener(
    "DOMContentLoaded",
    function() {


        // ----------------------------------------------
        // Boarding point search
        // ----------------------------------------------

        const searchButton =
            document.getElementById(
                "searchBusButton"
            );


        if (searchButton) {

            searchButton.addEventListener(
                "click",
                searchBoardingPoint
            );

        }


        const searchInput =
            document.getElementById(
                "boardingPoint"
            );


        if (searchInput) {

            searchInput.addEventListener(
                "keydown",
                function(event) {

                    if (
                        event.key === "Enter"
                    ) {

                        event.preventDefault();

                        searchBoardingPoint();

                    }

                }
            );

        }


        // ----------------------------------------------
        // Live bus Enter
        // ----------------------------------------------

        const noticeList =
            document.getElementById(
                "studentNoticeList"
            );

        if (noticeList) {
            loadPortalNotices("studentNoticeList");
        }


        const liveInput =
            document.getElementById(
                "liveBusNumber"
            );


        if (liveInput) {

            if (liveInput.tagName === "SELECT") {
                fetch("/api/buses")
                    .then(function(res) { return res.json(); })
                    .then(function(data) {
                        if (data.success && Array.isArray(data.buses) && data.buses.length > 0) {
                            liveInput.innerHTML = '<option value="">-- Choose Bus to Track --</option>';
                            data.buses.forEach(function(b) {
                                const opt = document.createElement("option");
                                opt.value = b.bus_number;
                                opt.textContent = "Bus " + b.bus_number + " - " + b.route_name;
                                liveInput.appendChild(opt);
                            });
                            if (window.currentUserAssignedBus) {
                                liveInput.value = window.currentUserAssignedBus;
                            }
                        }
                    })
                    .catch(function(err) {
                        console.error("Could not load trackable buses", err);
                    });

                liveInput.addEventListener("change", function() {
                    if (this.value) {
                        showLiveBus();
                    }
                });
            }

            liveInput.addEventListener(
                "keydown",
                function(event) {

                    if (
                        event.key === "Enter"
                    ) {

                        event.preventDefault();

                        showLiveBus();

                    }

                }
            );

        }

    }
);