# NCS 2.9 DHCP passes a non-terminated DNS array to the mDNS-sized resolver.
# Generate one reviewed source replacement in the build tree, preserving the SDK.
set(omi_dhcp_dir "${ZEPHYR_BASE}/subsys/net/lib/dhcpv4")
set(omi_dhcp_patch "${CMAKE_CURRENT_SOURCE_DIR}/../scripts/patch-ncs290-dhcp.py")
set(omi_dhcp_generated "${CMAKE_CURRENT_BINARY_DIR}/omi_dhcpv4.c")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${omi_dhcp_dir}/dhcpv4.c" "${omi_dhcp_patch}")
execute_process(COMMAND ${PYTHON_EXECUTABLE} "${omi_dhcp_patch}"
    "${omi_dhcp_dir}/dhcpv4.c" "${omi_dhcp_generated}"
    RESULT_VARIABLE omi_dhcp_result)
if(NOT omi_dhcp_result EQUAL 0)
    message(FATAL_ERROR "Could not apply the reviewed NCS 2.9 DHCP DNS fix")
endif()
get_target_property(omi_dhcp_sources subsys__net__lib__dhcpv4 SOURCES)
set(omi_dhcp_originals ${omi_dhcp_sources})
list(FILTER omi_dhcp_originals INCLUDE REGEX "(^|/)dhcpv4\\.c$")
list(LENGTH omi_dhcp_originals omi_dhcp_count)
if(NOT omi_dhcp_count EQUAL 1)
    message(FATAL_ERROR "Expected exactly one SDK DHCP client source")
endif()
list(FILTER omi_dhcp_sources EXCLUDE REGEX "(^|/)dhcpv4\\.c$")
set_property(TARGET subsys__net__lib__dhcpv4 PROPERTY SOURCES ${omi_dhcp_sources})
target_sources(subsys__net__lib__dhcpv4 PRIVATE "${omi_dhcp_generated}")
target_include_directories(subsys__net__lib__dhcpv4 PRIVATE "${omi_dhcp_dir}")
