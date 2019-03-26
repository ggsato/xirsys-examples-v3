<?php
    $curl = curl_init();
    curl_setopt_array( $curl, array (
        CURLOPT_URL => "https://global.xirsys.net/_host/".$_POST["channel"]."?type=signal&k=".$_POST["username"],
        CURLOPT_USERPWD => "ggsato:14f79f12-4161-11e9-b010-0242ac110003",
        CURLOPT_HTTPAUTH => CURLAUTH_BASIC,
        CURLOPT_CUSTOMREQUEST => "GET",
        CURLOPT_RETURNTRANSFER => 1
    ));
    $resp = curl_exec($curl);
    print $resp;
    curl_close($curl);
?>