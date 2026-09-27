cmake_minimum_required(VERSION 3.20.0)
# Nordic 2.9.0's macOS bundle ships an Intel-only protoc. Allow a native
# generator without changing the Nordic compiler/Python installation.
if(DEFINED ENV{OMI_PROTOC})
    set(PROTOBUF_PROTOC_EXECUTABLE "$ENV{OMI_PROTOC}" CACHE FILEPATH "Native protoc" FORCE)
endif()
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
enable_language(C ASM) # OPUS

project(omi)

file(GLOB app_sources
    src/main.c
    src/mic.c
    src/battery.c
    src/led.c
    src/haptic.c
    src/sd_card.c
    src/spi_flash.c
    src/settings.c
    src/feedback.c
    src/wdog_facade.c
    src/rtc.c
    src/imu.c
)

if(CONFIG_OMI_ENABLE_T5838_AAD)
    # Hardware AAD is integrated into mic.c; t5838_aad.c is the low-level driver.
    list(APPEND app_sources src/t5838_aad.c)
endif()
file(GLOB core_sources
    src/lib/core/config.h
    src/lib/core/codec.c
    src/lib/core/transport.c
    src/lib/core/button.c
    src/lib/core/monitor.c
)

if(CONFIG_OMI_ENABLE_OFFLINE_STORAGE)
    list(APPEND core_sources src/lib/core/storage.c)
endif()

if(CONFIG_OMI_WIFI_UPLOAD)
    # Keep Nordic's library target and generated protocol; replace only its
    # one-shot sample lifecycle with the CV1 browser/lifecycle adaptation.
    set(portal_header ${CMAKE_CURRENT_BINARY_DIR}/portal_page.h)
    add_custom_command(OUTPUT ${portal_header}
        COMMAND ${PYTHON_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/embed-portal.py
                ${CMAKE_CURRENT_SOURCE_DIR}/src/portal.html ${portal_header}
        DEPENDS src/portal.html ../scripts/embed-portal.py)
    add_custom_target(omi_portal_page DEPENDS ${portal_header})
    add_dependencies(softap_wifi_provision omi_portal_page)
    target_include_directories(softap_wifi_provision PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
    get_target_property(provision_sources softap_wifi_provision SOURCES)
    list(FILTER provision_sources EXCLUDE REGEX "(^|/)softap_wifi_provision\\.c$")
    set_property(TARGET softap_wifi_provision PROPERTY SOURCES "${provision_sources}")
    target_sources(softap_wifi_provision PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src/provisioning.c)
    target_include_directories(softap_wifi_provision PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    list(APPEND app_sources src/wifi_upload.c src/wifi_config.c src/wifi_radio.c)
    list(APPEND core_sources src/lib/core/local_auth.c)
endif()

target_sources(app PRIVATE ${core_sources} ${app_sources})
target_include_directories(app PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/lib/core
)

if(CONFIG_OMI_CODEC_OPUS)
    add_subdirectory(src/lib/core/lib/opus-1.2.1/)
    target_link_libraries(app PRIVATE opus_codec)
endif()
